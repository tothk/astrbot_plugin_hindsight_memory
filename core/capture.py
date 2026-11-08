"""把 AstrBot 事件转换成待记录的原始对话。

本模块是宿主感知层：它读取事件、调用守卫函数、解析人格，再把结果交给
Recorder 落库。它刻意不自己创建任何依赖，全部由 main.py 注入，
以便 main.py 只保留组装与转发的职责。
"""

from datetime import datetime
from typing import Awaitable, Callable

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.platform import MessageType
from astrbot.core.provider.entities import LLMResponse

from .persona import persona_for_reply, persona_from_conversation
from .recorder import (
    Logger,
    Recorder,
    build_raw_message,
    resolve_bot_id,
    should_record_reply,
    should_record_user_text,
)

NotifyFunc = Callable[[AstrMessageEvent], Awaitable[None]]


def reply_text(resp: LLMResponse) -> str:
    """取回复正文，非文本组件用占位符表示。

    LLMResponse.completion_text 走的是 MessageChain.get_plain_text()，
    默认 with_other_comps_mark=False —— 只收集 Plain 组件，图片、表情、
    @ 等全部被丢掉。后果是"发一张图"这类回复在记忆里不可见，纯图片
    回复更是变成空串被直接跳过、压根不入库。

    这里显式传 with_other_comps_mark=True，让图片变成 "[Image]"、
    表情变成 "[Face]" 之类的占位符，与用户侧的 get_message_outline()
    口径保持一致。

    Args:
        resp: 本次 agent 运行产生的响应。

    Returns:
        可写入记录的回复正文；没有 result_chain 时退回 completion_text。
    """
    if resp.result_chain:
        return resp.result_chain.get_plain_text(with_other_comps_mark=True)
    return resp.completion_text or ""


class MessageCapture:
    """负责「哪些消息要记、记哪些字段」的编排，具体落库交给 Recorder。"""

    def __init__(
        self,
        context: Context,
        recorder: Recorder,
        notify_failed: NotifyFunc,
        log: Logger,
    ) -> None:
        """初始化。

        Args:
            context: 插件上下文，用于解析人格。
            recorder: 负责落库与失败节流的记录器。
            notify_failed: 写入失败时的异步回调，接收触发本次记录的事件，
                由 main.py 提供，用于把提示发回给用户。
            log: 日志对象，通常是插件专属 logger。
        """
        self._context = context
        self._recorder = recorder
        self._notify_failed = notify_failed
        self._log = log

    async def capture_user(self, event: AstrMessageEvent) -> None:
        """记录用户发送的消息；不满足记录条件时静默返回。

        Args:
            event: 收到的消息事件。
        """
        message_str = event.get_message_outline()
        if not should_record_user_text(message_str):
            # 实测时最容易被误判成"漏记"的就是这里，打一行 debug 便于对账。
            self._log.debug(f"跳过用户消息（空文本或指令）: {message_str[:20]!r}")
            return
        persona_id = await persona_from_conversation(self._context, event)
        await self._recorder.save(
            build_raw_message(
                role="user",
                sender_id=event.get_sender_id(),
                text=message_str,
                umo=event.unified_msg_origin,
                persona_id=persona_id or "",
                now=datetime.now(),
            ),
            lambda: self._notify_failed(event),
        )

    async def capture_reply(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        """记录机器人的回复；错误、被打断、空回复都跳过。

        Args:
            event: 触发本次回复的消息事件。
            resp: 本次 agent 运行产生的响应。
        """
        # 先取正文再判守卫：纯图片回复经 reply_text() 后是 "[Image]" 而不是空串，
        # 不会被误判成空回复而漏记。
        text = reply_text(resp)
        if not should_record_reply(
            role=resp.role,
            text=text,
            is_private=event.get_message_type() == MessageType.FRIEND_MESSAGE,
            stopped=event.is_stopped(),
            stop_requested=bool(event.get_extra("agent_stop_requested")),
        ):
            self._log.debug(
                f"跳过回复记录 role={resp.role} "
                f"stopped={event.is_stopped()} "
                f"len={len(text)}"
            )
            return
        bot_id = resolve_bot_id(
            event.message_obj.self_id,
            event.get_platform_id(),
        )
        persona_id = await persona_for_reply(self._context, event)
        await self._recorder.save(
            build_raw_message(
                role="llm",
                sender_id=bot_id,
                text=text,
                umo=event.unified_msg_origin,
                persona_id=persona_id or "",
                now=datetime.now(),
            ),
            lambda: self._notify_failed(event),
        )
