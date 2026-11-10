"""在对话前从 Hindsight 召回记忆并拼成可注入的文本。

本模块不 import astrbot：HTTP 客户端与日志都由调用方注入，便于直接单测。

召回策略：一次 recall 请求取回全部类型，再按「observation 优先」排序后截断。
之所以不做两次请求（先只取 observation、不够再补），是因为每次 recall 都意味着
一次向量检索 + 重排，成本远高于多拿回几条结果再在本地排。
"""

import asyncio

from ..hindsight.client import HindsightClient, HindsightError

# observation 是 Hindsight 从多条事实归纳出的稳定结论（例如「她最近工作压力大」），
# 比零散的一句 world 事实更适合放进对话上下文，所以优先。
_PREFERRED_TYPE = "observation"

# 单次召回的硬超时。主回复的延迟不能被记忆检索拖住：宁可这轮没有记忆，
# 也不能让用户等待。Hindsight 冷启动首次检索可能要几秒。
_DEFAULT_TIMEOUT = 3.0

# 注入到系统提示里的标题。用纯文本而不是 JSON，是为了让模型读起来像背景信息
# 而不是结构化数据——后者容易被它当成待处理的任务。
_HEADER = "以下是你对该用户既往对话的记忆片段，仅供参考，不要逐条复述："


def format_memories(results: list[dict], limit: int) -> str:
    """把召回结果整理成注入文本。

    Args:
        results: ``recall`` 返回的 ``results`` 列表。
        limit: 最多保留几条，对应配置项 ``recall_memory_quantity``。

    Returns:
        可直接追加到系统提示的文本；没有可用记忆时返回空串。
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
        可注入系统提示的文本；没有记忆或出错时返回空串。
    """
    query = query.strip()
    if not query or quantity <= 0:
        return ""

    try:
        data = await asyncio.wait_for(
            client.recall(bank_id, query, max_tokens=1024),
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
