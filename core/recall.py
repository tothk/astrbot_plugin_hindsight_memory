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

# observation 是 Hindsight 从多条事实归纳出的稳定结论（例如「她最近工作压力大」），
# 比零散的一句 world 事实更适合放进对话上下文，所以优先。
_PREFERRED_TYPE = "observation"

# 单次召回的硬超时。主回复的延迟不能被记忆检索拖住：宁可这轮没有记忆，
# 也不能让用户等待。Hindsight 冷启动首次检索可能要几秒。
_DEFAULT_TIMEOUT = 3.0

# 注入到用户消息里的标题。用纯文本而不是 JSON，是为了让模型读起来像背景信息
# 而不是结构化数据——后者容易被它当成待处理的任务。
# 这段文字最终出现在**用户消息**里（见 main.py 的 on_llm_request），所以用
# 「【相关记忆】」这种块标记把边界标清楚，避免模型把记忆内容当成用户刚说的话。
_HEADER = "【相关记忆】以下是你们过往对话中与当前话题相关的片段，仅供参考，不要逐条复述："


def format_memories(results: list[dict], limit: int) -> str:
    """把召回结果整理成注入文本。

    Args:
        results: ``recall`` 返回的 ``results`` 列表。
        limit: 最多保留几条，对应配置项 ``recall_memory_quantity``。

    Returns:
        可直接注入到用户消息里的文本；没有可用记忆时返回空串。
    """
    if limit <= 0:
        return ""

    # 稳定排序：observation 排前面，同类型内保持服务端给出的相关性顺序。
    ordered = sorted(
        results,
        key=lambda item: 0 if item.get("type") == _PREFERRED_TYPE else 1,
    )

    lines: list[str] = []
    for item in ordered:
        text = str(item.get("text") or "").strip()
        if not text:
            # observation 之外的条目偶尔没有正文（只有结构化字段），跳过。
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
        return await client.recall(bank_id, query, max_tokens=1024)
    except HindsightBankNotFoundError:
        # 走到这里说明库确实不存在（404 是服务端明确告知的），
        # 建库后立刻重试，用户这一轮就能拿到记忆，不必等下一轮。
        await client.ensure_bank(bank_id)
        log.info(f"记忆库 {bank_id!r} 不存在，已按默认配置创建")
        return await client.recall(bank_id, query, max_tokens=1024)


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
