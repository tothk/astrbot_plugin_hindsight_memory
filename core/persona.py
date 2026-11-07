from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context


async def persona_from_conversation(context: Context, event: AstrMessageEvent) -> str:
    """回查当前会话的人格 ID，失败或未设置返回空串。"""
    try:
        conversation_manager = context.conversation_manager
        curr_cid = await conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        conversation = (
            await conversation_manager.get_conversation(
                event.unified_msg_origin, curr_cid
            )
            if curr_cid
            else None
        )
        persona_id, _, _, _ = await context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=(
                conversation.persona_id if conversation else None
            ),
            platform_name=event.get_platform_name(),
            provider_settings=context.get_config(event.unified_msg_origin).get(
                "provider_settings", {}
            ),
        )
        return persona_id or ""
    except Exception as e:
        logger.error(f"解析人格失败，先按空值记录: {e}")
        return ""


async def persona_for_reply(context: Context, event: AstrMessageEvent) -> str:
    """回复侧用：优先取 agent 已解析的结果，取不到再回查会话。"""
    persona_id = event.get_extra("_trace_persona_id")
    if persona_id:
        return persona_id
    return await persona_from_conversation(context, event)