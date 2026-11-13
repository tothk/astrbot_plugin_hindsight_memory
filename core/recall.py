"""在对话前从 Hindsight 召回记忆并拼成可注入的文本。

本模块不 import astrbot：HTTP 客户端与日志都由调用方注入，便于直接单测。

召回策略：一次 recall 请求取回全部类型，再按「observation 优先」排序后截断。
之所以不做两次请求（先只取 observation、不够再补），是因为每次 recall 都意味着
一次向量检索 + 重排，成本远高于多拿回几条结果再在本地排。

记忆库不存在时会先创建再重试一次（见 ``_recall_or_create_bank``）：服务端不会
因为一次召回就自动建库，而首次安装的插件库里必然是空的。
"""

import asyncio

from ..hindsight.client import HindsightBankNotFoundError, HindsightClient, HindsightError

# 只召回 observation。
#
# observation 是 Hindsight 从多条事实归纳出的稳定结论（例如「她最近工作压力大」），
# 比零散的一句 world 事实更适合放进对话上下文。
#
# 关键点是这个过滤交给**服务端**做，而不是拿回来再挑：`types` 直接参与检索，
# 四路（semantic/bm25/graph/temporal）只对指定 fact_type 跑，RRF 融合与
# cross-encoder 重排的候选池从一开始就只剩 observation。改成客户端过滤的话，
# 重排照样要处理全部类型的候选——生产库上实测单次召回的重排输入能到 160K token，
# 这正是召回超时的直接原因。
#
# 代价：observation 由服务端后台的 consolidation 任务归纳产出，新库刚灌完数据
# 时可能还没有，那几轮召回会是空的。这是有意的取舍——宁可这轮没有记忆，
# 也不要把零散事实塞进上下文。
_RECALL_TYPES = ["observation"]

# 单次召回的硬超时。主回复的延迟不能被记忆检索拖住：宁可这轮没有记忆，
# 也不能让用户等待。Hindsight 冷启动首次检索可能要几秒。
_DEFAULT_TIMEOUT = 3.0

# 召回深度。服务端默认是 mid（候选池 300 条、图遍历也更深），实测在
# 一对一的陪伴场景里会稳定跑满 3 秒超时，这一轮的记忆就白丢了。
# low 对应候选池 100 条——我们每轮最终只取 5 条（recall_memory_quantity），
# 100 条候选足够排在前面的几条浮出来，而搜索耗时降一大截。
# 参考 docs/hindsight-api.md 5.4 的推荐组合：聊天机器人回复用 low。
_RECALL_BUDGET = "low"

# 注入到用户消息里的标题。用纯文本而不是 JSON，是为了让模型读起来像背景信息
# 而不是结构化数据——后者容易被它当成待处理的任务。
# 这段文字最终出现在**用户消息**里（见 main.py 的 on_llm_request），所以用
# 「【相关记忆】」这种块标记把边界标清楚，避免模型把记忆内容当成用户刚说的话。
_HEADER = "【相关记忆】以下是你们过往对话中与当前话题相关的片段，仅供参考，不要逐条复述："


def _is_memory_part(part: object) -> bool:
    """判断一个 content part 是不是本模块注入的记忆片段。

    只认页眉前缀，不认 ``_no_save``：后者 AstrBot 自带的知识库注入也在用
    （``astr_main_agent/knowledge_base.py``），按它过滤会连别人的内容一起删掉。

    ``getattr`` 那层是防别的 part 类型：``image_url``/``audio_url``/``think``
    没有 ``text`` 字段，直接取属性会 ``AttributeError``。

    Args:
        part: 待判定的 content part，鸭子类型即可。

    Returns:
        是记忆片段返回 True。
    """
    text = getattr(part, "text", None)
    return isinstance(text, str) and text.startswith(_HEADER)


def prune_previous_memories(parts: list) -> int:
    """就地移除上一次注入的记忆片段，为本次注入腾位置。

    同一个请求被处理两遍时（热重载导致 handler 重复注册、或将来某个 runner
    重复触发钩子），裸 ``append`` 会让用户消息里叠出两段「【相关记忆】」，
    同一份背景被模型读两遍、白烧 token。

    **必须就地改**（``parts[:] = ...``）而不是重新绑定：tool 循环的重查询
    直接持有 ``req.extra_user_content_parts`` 这个列表对象
    （``tool_loop_agent_runner`` 里 ``extra_user_content_parts=self.req....``），
    重新绑定会让它继续用那份带旧片段的列表。

    Args:
        parts: ``ProviderRequest.extra_user_content_parts``。

    Returns:
        移除的片段条数。
    """
    kept = [part for part in parts if not _is_memory_part(part)]
    removed = len(parts) - len(kept)
    parts[:] = kept
    return removed


def format_memories(results: list[dict], limit: int) -> str:
    """把召回结果整理成注入文本。

    服务端已经按 ``types`` 只返回 observation，且结果按相关性排好序，
    所以这里按原顺序取前 ``limit`` 条即可，不做重排。

    Args:
        results: ``recall`` 返回的 ``results`` 列表。
        limit: 最多保留几条，对应配置项 ``recall_memory_quantity``。

    Returns:
        可直接注入到用户消息里的文本；没有可用记忆时返回空串。
    """
    if limit <= 0:
        return ""

    lines: list[str] = []
    for item in results:
        text = str(item.get("text") or "").strip()
        if not text:
            # 极少数记忆只有结构化字段、没有正文，跳过而不是留一个空项目符。
            continue
        lines.append(f"- {text}")
        if len(lines) >= limit:
            break

    if not lines:
        return ""
    return "\n".join([_HEADER, *lines])


async def _recall_or_create_bank(
        client: HindsightClient, bank_id: str, query: str, log
) -> dict:
    """召回记忆；库不存在时先建库再重试一次。

    服务端不会因为召回就自动建库，空库会稳定返回 404。首次安装的插件
    因此永远拿不到记忆，日志里只有一行看不懂的 ``Bank 'default' not found``。

    建库只做一次重试：如果建完还 404（PUT 被拒或权限不足），说明问题不在
    「库缺失」上，再重试只会变成死循环。

    Args:
        client: Hindsight 客户端。
        bank_id: 记忆库标识。
        query: 用户消息。
        log: 日志对象。

    Returns:
        ``recall`` 的原始响应。

    Raises:
        HindsightBankNotFoundError: 建库后仍然找不到库。
        HindsightError: 其它请求失败。
    """
    try:
        return await client.recall(
            bank_id, query, types=_RECALL_TYPES, budget=_RECALL_BUDGET
        )
    except HindsightBankNotFoundError:
        # 走到这里说明库确实不存在（404 是服务端明确告知的），
        # 建库后立刻重试，用户这一轮就能拿到记忆，不必等下一轮。
        # 重试也要带全参数：首次安装必然走这条分支，漏了就等于那一轮没用新策略。
        await client.ensure_bank(bank_id)
        log.info(f"记忆库 {bank_id!r} 不存在，已按默认配置创建")
        return await client.recall(
            bank_id, query, types=_RECALL_TYPES, budget=_RECALL_BUDGET
        )


async def recall_memories(
        client: HindsightClient,
        bank_id: str,
        query: str,
        quantity: int,
        log,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """召回与当前消息相关的记忆。

    任何失败都返回空串：记忆是可选的增强，不能因为它让对话失败或变慢。

    Args:
        client: 已配置好的 Hindsight 客户端。
        bank_id: 记忆库标识。
        query: 用户这一轮的原始消息。
        quantity: 最多注入几条。
        log: 日志对象，需提供 debug/warning。
        timeout: 整体超时秒数。

    Returns:
        可注入用户消息的文本；没有记忆或出错时返回空串。
    """
    query = query.strip()
    if not query or quantity <= 0:
        return ""

    try:
        data = await asyncio.wait_for(
            _recall_or_create_bank(client, bank_id, query, log),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # 超时很常见（冷启动、服务端在做重排），只记 debug，避免刷屏。
        log.debug(f"记忆召回超时（>{timeout}s），本轮不注入记忆")
        return ""
    except HindsightError as e:
        log.warning(f"记忆召回失败：{e}")
        return ""

    # source_facts 可能为 None，统一按空列表处理。
    text = format_memories(data.get("results") or [], quantity)
    if text:
        log.debug(f"已召回记忆 {text.count('- ')} 条用于本轮对话")
    else:
        log.debug("本轮没有命中记忆")
    return text
