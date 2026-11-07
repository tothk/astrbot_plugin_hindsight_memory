"""原始对话记录的装配与落库。

本模块刻意不 import astrbot：数据库、通知回调、日志函数全部由调用方注入，
因此可以在没有 AstrBot 宿主的环境里直接 import，便于单元测试。
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

from ..db.models.models import RawMessage

# 只为类型注解导入，运行时不需要真正加载 SqlModelManager，
# 避免本模块被导入时顺带拉起引擎相关的依赖。
if TYPE_CHECKING:
    from ..db.models.sql_model_manager import SqlModelManager


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
    day = now.date().isoformat()
    return RawMessage(
        record_date=day,
        # 文档边界是一天：同一天同一会话的记录归入同一个 document_id，
        # 便于下游按天提取事实。umo 自带冒号，故这里用冒号分隔不会破坏解析。
        document_id=f"{day}:{umo}",
        sender_id=sender_id,
        persona_id=persona_id,
        role=role,
        raw_message=text,
        message_sent_time=now.astimezone().isoformat(timespec="seconds"),
    )


class Recorder:
    """把记录写入数据库；失败时仅在首次失败通知一次。

    "只通知一次" 是为了避免 database is locked 这类持续性故障刷屏：
    每句对话会触发两次写入（user 行与 llm 行），逐条提示会成倍放大。
    一旦写入成功，_warned 复位，下次故障可以再次通知。
    """

    def __init__(self, sql: "SqlModelManager", log: Callable[[str], None]) -> None:
        """初始化。

        Args:
            sql: 提供 insert_raw 的数据库管理器。
            log: 日志函数，通常是 logger.error。
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
            await asyncio.to_thread(self._sql.insert_raw, raw)
        except Exception as e:
            self._log(f"写入原始记录失败: {e}")
            if not self._warned:
                self._warned = True
                # 记录失败对陪伴场景不可接受，必须让用户看见；但只提示第一次。
                await notify()
            return False
        self._warned = False
        return True
