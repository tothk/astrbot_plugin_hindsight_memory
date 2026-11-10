"""把 AstrBot 事件转换成待记录的原始对话。

本模块是宿主感知层：它读取事件、调用守卫函数、解析人格，再把结果交给
Recorder 落库。它刻意不自己创建任何依赖，全部由 main.py 注入，
以便 main.py 只保留组装与转发的职责。
"""

from datetime import datetime
from typing import Awaitable, Callable, Sequence

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.platform import MessageType
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter

from .persona import persona_for_reply, persona_from_conversation
from .recorder import (
    Logger,
    Recorder,
    build_raw_message,
    resolve_bot_id,
    should_record_reply,
    should_record_user_text,
    strip_wake_prefix,
)

NotifyFunc = Callable[[AstrMessageEvent], Awaitable[None]]

# AstrBot 全局默认值（astrbot/core/config/default.py:306），
# 读配置失败时按它兜底，与宿主行为保持一致。
_DEFAULT_WAKE_PREFIX = ("/",)

# 机器人自己那一侧拿不到昵称时的显示名。
#
# 投递给 Hindsight 的对话文本里，说话人名字会被解析成实体；用 self_id 或
# 平台 ID 的话会退化成没有意义的实体，所以这里给一个固定的中文称呼。
_DEFAULT_BOT_NAME = "助手"


def bot_display_name(context: Context, persona_id: str) -> str:
    """取机器人当前人格的显示名，用作对话文本里的说话人。

    Args:
        context: 插件上下文，用于查人格。
        persona_id: 本轮生效的人格 ID，未知时为空串。

    Returns:
        人格名；取不到就退回 ``_DEFAULT_BOT_NAME``。
    """
    if not persona_id:
        return _DEFAULT_BOT_NAME
    try:
        # 这个方法在宿主里是同步的纯内存查找（persona_mgr.get_persona_v3_by_id），
        # 不会读库也不会发网络请求，放在钩子里没有负担。
        persona = context.persona_manager.get_persona_v3_by_id(persona_id)
    except Exception:
        # 人格名只是显示用，取不到不该影响记录本身。
        return _DEFAULT_BOT_NAME
    name = getattr(persona, "name", "") if persona is not None else ""
    return str(name).strip() or _DEFAULT_BOT_NAME


def read_wake_prefixes(
    context: Context,
    event: AstrMessageEvent,
    log: Logger,
) -> Sequence[str]:
    """读取当前会话生效的唤醒前缀。

    前缀是宿主级配置（不是本插件配置），按会话取值以支持对不同平台
    设置不同前缀；取不到时退回 AstrBot 的默认值。

    Args:
        context: 插件上下文，用于读取配置。
        event: 当前消息事件，用于确定会话。
        log: 日志对象，仅在读取失败时使用。

    Returns:
        唤醒前缀元组，可能是空元组（表示不靠前缀唤醒）。
    """
    try:
        config = context.get_config(event.unified_msg_origin)
        prefixes = config.get("wake_prefix", _DEFAULT_WAKE_PREFIX)
    except Exception as e:
        # 配置读不到只在记录口径上做退让，不该影响主流程。
        log.debug(f"读取 wake_prefix 失败，按默认值 {_DEFAULT_WAKE_PREFIX} 处理: {e}")
        return _DEFAULT_WAKE_PREFIX
    if isinstance(prefixes, str):
        return (prefixes,)
    return tuple(prefixes)


# 这三类过滤器代表"这是一个指令"：命令、命令组、正则匹配。
# 与 astrbot/core/pipeline/process_stage/method/star_request.py 判定
# handler_category 用的是同一组类型。
_COMMAND_FILTERS = (CommandFilter, CommandGroupFilter, RegexFilter)


def is_command_event(event: AstrMessageEvent) -> bool:
    """判断这条消息是否命中了某个指令处理器。

    不能用"正文是否以 / 开头"来判定：唤醒前缀会被剥掉，剥完之后
    "/reset" 和"/今天好累"都是不带斜杠的正文，文本层面已经分不出来。
    可靠的做法是看宿主在唤醒检查阶段留下的痕迹 —— 命中的 handler 会被
    写进 event 的 activated_handlers（astrbot/core/pipeline/waking_check/
    stage.py:268），其中只要有一个带指令类过滤器，这条消息就是指令。

    注意 RegexFilter 也算指令：正则处理器在宿主里同样被归为
    handler_category == "regex"，走的是"不进 LLM"的分支。

    Args:
        event: 收到的消息事件。

    Returns:
        True 表示这条消息应视为指令、不该写入记忆。
    """
    handlers = event.get_extra("activated_handlers") or ()
    for handler in handlers:
        for event_filter in getattr(handler, "event_filters", ()):
            if isinstance(event_filter, _COMMAND_FILTERS):
                return True
    return False


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
        # get_message_outline() 读的是原始消息链，wake_prefix 只被 AstrBot
        # 从 event.message_str 上剥掉，所以这里要自己再剥一次。
        message_str = strip_wake_prefix(
            event.get_message_outline(),
            read_wake_prefixes(self._context, event, self._log),
        )
        is_command = is_command_event(event)
        if not should_record_user_text(message_str, is_command=is_command):
            # 实测时最容易被误判成"漏记"的就是这里，打一行 debug 便于对账。
            self._log.debug(
                f"跳过用户消息（空文本或指令）: is_command={is_command} "
                f"{message_str[:20]!r}"
            )
            return
        persona_id = await persona_from_conversation(self._context, event)
        await self._recorder.save(
            build_raw_message(
                role="user",
                sender_id=event.get_sender_id(),
                # 记下昵称：投递 Hindsight 时用它当说话人，数字 ID 会退化成 "user"。
                sender_name=event.get_sender_name(),
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
                # 机器人这一侧没有昵称，用当前人格名当说话人；
                # 用 self_id 的话 Hindsight 会把它解析成没有意义的实体。
                sender_name=bot_display_name(self._context, persona_id),
                text=text,
                umo=event.unified_msg_origin,
                persona_id=persona_id or "",
                now=datetime.now(),
            ),
            lambda: self._notify_failed(event),
        )
