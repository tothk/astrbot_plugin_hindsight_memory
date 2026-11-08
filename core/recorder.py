"""原始对话记录的装配与落库。

本模块刻意不 import astrbot：数据库、通知回调、日志函数全部由调用方注入，
因此可以在没有 AstrBot 宿主的环境里直接 import，便于单元测试。
"""

import asyncio
import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

from ..db.models.models import RawMessage

# 只为类型注解导入，运行时不需要真正加载 SqlModelManager，
# 避免本模块被导入时顺带拉起引擎相关的依赖。
if TYPE_CHECKING:
    from ..db.models.sql_model_manager import SqlModelManager


# 用户打断生成时，AstrBot 会造一条 role="assistant"、正文恰好是这个字符串的
# 伪回复（astrbot/core/agent/runners/tool_loop_agent_runner.py:118 的
# USER_INTERRUPTION_MESSAGE，由 :1573 _finalize_aborted_step() 构造），
# 并且照样调用 on_agent_done 通知插件 —— 也就是说它会以正常回复的身份
# 到达 on_llm_response 钩子。
#
# 光靠 event.is_stopped() / agent_stop_requested 挡不住：钩子被调用的时刻，
# astrbot/core/astr_agent_run_util.py 的 aborted 分支已经 clear_result() 清掉
# stopped、并把 agent_stop_requested 复位成 False，插件看到的两个信号都可能是假。
# 所以这里再加一条纯文本判据：正文正好是这句占位符，就一定不是模型说的话。
USER_INTERRUPTION_MESSAGE = "Output stopped."


class Logger(Protocol):
    """本模块用到的最小日志接口。

    astrbot 的 logger 是 LoggerAdapter，标准库的 Logger 也满足这个形状。
    用 Protocol 而不是直接 import astrbot，是为了让本模块保持无宿主依赖。
    """

    def debug(self, msg: str) -> None: ...

    def info(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...


def should_record_user_text(text: str) -> bool:
    """判断一条用户消息是否应当记录。

    空文本（纯图片等非文本消息）与指令不记录：指令属于操作行为，
    写进记忆会污染下游的事实提取。

    Args:
        text: 经过 get_message_outline() 转换后的消息正文。

    Returns:
        True 表示应当记录。
    """
    return text != "" and not text.startswith(("/", "／"))


def should_record_reply(
    *,
    role: str,
    text: str,
    is_private: bool,
    stopped: bool,
    stop_requested: bool,
) -> bool:
    """判断一条机器人回复是否应当记录。

    需要排除五种情况：出错回复（role 为 "err"）、被用户打断的回复
    （此时 completion_text 是占位文本 "Output stopped."）、空回复、
    非私聊会话，以及事件上残留的停止信号。

    Args:
        role: LLMResponse.role。
        text: 经过 reply_text() 取到的回复正文。
        is_private: 是否私聊会话。
        stopped: 事件是否已被停止。
        stop_requested: 是否被标记了停止请求。

    Returns:
        True 表示应当记录。
    """
    if not is_private or role == "err" or not text:
        return False
    # 占位文本判定放在停止信号之前：符号信号不可靠时这条仍然成立。
    if text.strip() == USER_INTERRUPTION_MESSAGE:
        return False
    return not (stopped or stop_requested)


def resolve_bot_id(self_id: str, platform_id: str) -> str:
    """确定机器人自身的 ID。

    部分适配器拿不到 self_id，会返回空串或固定的 unknown_selfid，
    此时退回平台适配器 ID 占位。

    Args:
        self_id: event.message_obj.self_id。
        platform_id: event.get_platform_id()。

    Returns:
        可写入记录的机器人标识。
    """
    if not self_id or self_id == "unknown_selfid":
        return platform_id
    return self_id


def content_hash(document_id: str, role: str, text: str) -> str:
    """计算一条记录的去重指纹。

    只纳入"消息本身"的属性：同一天同一会话（document_id）、说话方（role）、
    正文（text）。时间与人格刻意排除——message_sent_time 是写入时刻，
    重写同一事件会得到不同的秒数，纳入后去重永远不会命中；
    persona_id 是派生值，可能为空也可能事后被修正。

    代价：同一天同一会话里两段完全相同的正文会被判为重复而丢弃。

    Args:
        document_id: 文档标识，含日期与会话。
        role: 说话方，"user" 或 "llm"。
        text: 消息正文。

    Returns:
        十六进制的 sha256 摘要。
    """
    # 用 \x1f（ASCII 单元分隔符）拼接，避免字段内容自身含分隔符时产生歧义。
    raw = f"{document_id}\x1f{role}\x1f{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_raw_message(
    *,
    role: str,
    sender_id: str,
    text: str,
    umo: str,
    persona_id: str,
    now: datetime,
) -> RawMessage:
    """把一次对话装配成待落库的记录。

    Args:
        role: 说话方，"user" 或 "llm"。
        sender_id: 发送者 ID，llm 行为机器人 ID。
        text: 消息正文。
        umo: 会话统一消息来源，形如 "platform:type:session_id"。
        persona_id: 人格 ID，未知时传空串。
        now: 记录时刻，由调用方传入以便测试（测试可传固定时间断言字段值）。

    Returns:
        尚未落库的 RawMessage。
    """
    # 先换算到本机时区，再取日期：否则传进来的 now 若带别的时区（例如 UTC），
    # record_date 会按那个时区的日期算，而 message_sent_time 却是本机时间，
    # 跨零点的消息就会出现「日期是昨天、时间戳是今天」的自相矛盾。
    local_now = now.astimezone()
    day = local_now.date().isoformat()
    # 文档边界是一天：同一天同一会话的记录归入同一个 document_id，
    # 便于下游按天提取事实。umo 自带冒号，故这里用冒号分隔不会破坏解析。
    document_id = f"{day}:{umo}"
    return RawMessage(
        record_date=day,
        document_id=document_id,
        sender_id=sender_id,
        persona_id=persona_id,
        role=role,
        raw_message=text,
        message_sent_time=local_now.isoformat(timespec="seconds"),
        content_hash=content_hash(document_id, role, text),
    )


class Recorder:
    """把记录写入数据库；失败时仅在首次失败通知一次。

    "只通知一次" 是为了避免 database is locked 这类持续性故障刷屏：
    每句对话会触发两次写入（user 行与 llm 行），逐条提示会成倍放大。
    一旦写入成功，_warned 复位，下次故障可以再次通知。
    """

    def __init__(self, sql: "SqlModelManager", log: Logger) -> None:
        """初始化。

        Args:
            sql: 提供 insert_raw 的数据库管理器。
            log: 日志对象，通常是 astrbot 的 logger。按对象注入而不是直接
                import astrbot.api，这样本模块仍可在没有宿主的测试里使用。
        """
        self._sql = sql
        self._log = log
        self._warned = False

    async def save(self, raw: RawMessage, notify: Callable[[], Awaitable[None]]) -> bool:
        """写入一条记录。

        写入放到工作线程执行，避免抢 sqlite 写锁时阻塞事件循环。

        Args:
            raw: 待落库的记录。
            notify: 失败时调用的异步通知回调，只在首次失败时触发。

        Returns:
            True 表示写入成功，False 表示失败。
        """
        try:
            # 同步的 Session + commit 不能直接放在事件循环里：
            # 抢不到 sqlite 写锁时会阻塞整个线程（最长 connect_args timeout 秒），
            # 连心跳与其它会话一起停摆，所以移到工作线程执行。
            # 内容重复时 insert_raw 返回 None（唯一索引挡下），不是故障。
            saved = await asyncio.to_thread(self._sql.insert_raw, raw)
        except Exception as e:
            self._log.error(f"写入原始记录失败: {e}")
            if not self._warned:
                self._warned = True
                # 记录失败对陪伴场景不可接受，必须让用户看见；但只提示第一次。
                await notify()
            return False
        if saved is None:
            # 同一天同一会话里内容完全相同的消息会被判重丢弃，这是设计行为。
            # 打到 info 是为了实测时能对上"少了几行"，不必怀疑是漏记。
            self._log.info(
                f"内容重复，跳过写入 [{raw.role}] {raw.document_id} "
                f"{raw.raw_message[:20]!r}"
            )
            self._warned = False
            return True
        self._log.info(
            f"已记录 [{raw.role}] {raw.document_id} "
            f"id={saved.id} persona={raw.persona_id or '(空)'} "
            f"len={len(raw.raw_message)}"
        )
        self._warned = False
        return True
