"""在对话前从 Hindsight 召回记忆并拼成可注入的文本。

本模块不 import astrbot：HTTP 客户端与日志都由调用方注入，便于直接单测。

召回策略：只向服务端要 ``observation`` 类型（过滤在检索阶段生效，见
``_RECALL_TYPES``），拿回来按原顺序截断即可，不做本地重排。

记忆库不存在时**不在热路径建库**（见 ``_recall_or_create_bank``）：库里没有东西，
这一轮本来也召不到记忆，没必要为了建库让用户多等几次往返。建库丢到后台做，
下一轮自然就有库了。
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

# 单次 HTTP 请求的上限，必须**小于** ``_DEFAULT_TIMEOUT``。
#
# 外层 wait_for 一到期就把整段召回取消，而那时 httpx 请求还在飞——日志里只剩
# 一句「超时」，看不出是哪个请求慢。把请求本身的超时压在下面，超时会由 HTTP
# 层报出来（带 URL、带真实耗时）。留 0.5 秒给事件循环的调度余量。
_REQUEST_TIMEOUT = 2.5

# 后台建库的超时。它不占用户的时间，可以放宽，但也不能真的挂着不结束。
_ENSURE_BANK_TIMEOUT = 5.0

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


async def _ensure_bank_in_background(
        client: HindsightClient, bank_id: str, log
) -> None:
    """在后台把记忆库建出来，失败只记日志。

    没人 await 这个协程，所以异常必须自己吞掉：逃出去只会变成
    ``Task exception was never retrieved`` 噪声，而且下次调用又是干净的。

    Args:
        client: Hindsight 客户端。
        bank_id: 记忆库标识。
        log: 日志对象。
    """
    try:
        await asyncio.wait_for(
            client.ensure_bank(bank_id), timeout=_ENSURE_BANK_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning(
            f"后台建库超时（>{_ENSURE_BANK_TIMEOUT:g}s）：{bank_id!r}，下一轮召回时再试"
        )
    except HindsightError as e:
        log.warning(f"后台建库失败：{bank_id!r}：{e}")
    else:
        log.info(f"记忆库 {bank_id!r} 不存在，已在后台按默认配置创建")


async def _recall_or_create_bank(
        client: HindsightClient, bank_id: str, query: str, log
) -> dict:
    """召回记忆；库不存在时把建库丢到后台，本轮按「没有记忆」处理。

    服务端不会因为召回就自动建库，空库会稳定返回 404。首次安装的插件因此
    永远拿不到记忆——但**不能**在这里同步建库再重试：那条路径要走
    recall(404) → GET 查存在性 → PUT 建库 → recall 重试四次往返，全挤在
    ``recall_memories`` 那 3 秒里，首次安装那一轮必然超时，而且因为外层
    wait_for 先到、超时只在 debug 里留一句，表现成「有记录但 AI 记不住」。

    建库交给后台任务的原因：库里没有东西时，这一轮本来也召不到记忆，
    为用户省下这几次往返比让他等一个空结果更划算。下一轮召回时库已经在了。

    Args:
        client: Hindsight 客户端。
        bank_id: 记忆库标识。
        query: 用户消息。
        log: 日志对象。

    Returns:
        ``recall`` 的原始响应；库不存在时返回空结果。

    Raises:
        HindsightError: 其它请求失败（404 已在内部消化）。
    """
    try:
        return await client.recall(
            bank_id,
            query,
            types=_RECALL_TYPES,
            budget=_RECALL_BUDGET,
            timeout=_REQUEST_TIMEOUT,
        )
    except HindsightBankNotFoundError:
        # 库里一条记忆都没有，本轮注定召不到东西。建库是后台的事。
        try:
            asyncio.get_running_loop().create_task(
                _ensure_bank_in_background(client, bank_id, log)
            )
        except RuntimeError:
            # 没有运行中的事件循环（同步上下文里被调用）：建库只能放弃，
            # 但召回本身已经拿到了确定答案，不该因此报错。
            log.debug(f"没有运行中的事件循环，跳过建库：{bank_id!r}")
        return {"results": []}


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
        timeout: 整体超时秒数。必须大于 :data:`_REQUEST_TIMEOUT`，否则
            外层 wait_for 会先掐断正在飞的 HTTP 请求。

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
