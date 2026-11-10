"""把「记录 → 投递 → 召回」编排成插件可用的一个服务对象。

职责边界：

- ``MessageCapture`` 只管把消息变成库里的行（同步侧）。
- ``HindsightSync`` 只管把行投递给远端。
- 本模块负责把它们和配置、定时任务、HTTP 客户端生命周期接起来，
  并对外提供两个动作：``recall_for(text)`` 与 ``sync_once()``。

本模块 import astrbot 仅用于类型注解，实际依赖全部由 main.py 注入。
"""

import asyncio
from datetime import datetime, time, timedelta

from ..hindsight.client import HindsightClient
from ..hindsight.plugin_config import HindsightConfig
from ..hindsight.sync import HindsightSync, SyncOutcome
from .recall import recall_memories

# 每日补投的默认时刻（配置 sync_daily_time 解析失败时使用）。
_DEFAULT_DAILY = time(hour=0, minute=5)

# 定时任务的兜底间隔：如果算出的等待时间不合理（时钟回拨、配置写错），
# 也不要让任务睡上几天或负值空转。
_MIN_SLEEP = 60.0


def parse_daily_time(raw: str) -> time:
    """解析 ``HH:MM`` 形式的每日时刻。

    Args:
        raw: 配置值，例如 ``"00:05"``。

    Returns:
        解析出的 ``time``；格式不合法时返回默认的 00:05 而不是报错——
        一个手滑的配置不应该让整个插件起不来。
    """
    try:
        hour_text, minute_text = str(raw).strip().split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (ValueError, AttributeError):
        return _DEFAULT_DAILY


def seconds_until(target: time, now: datetime | None = None) -> float:
    """算出距离下一次 ``target`` 还有多少秒。

    Args:
        target: 每日的目标时刻（本机时区）。
        now: 当前时刻，测试可注入。

    Returns:
        下次触发前的秒数，恒为正。
    """
    current = now or datetime.now()
    today_target = current.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if today_target <= current:
        today_target += timedelta(days=1)
    return (today_target - current).total_seconds()


def format_sync_result(outcome: SyncOutcome) -> str:
    """把一轮同步的结果渲染成给用户看的一句话。

    单独抽成纯函数：手动指令要回复用户，而拼文案这种细节不该混进业务逻辑，
    也方便直接单测（不需要真的连数据库和 Hindsight）。

    Args:
        outcome: ``HindsightSync.run()`` 的返回值。

    Returns:
        一行中文摘要。
    """
    if not outcome.ok:
        return f"❌ 同步失败：{outcome.error}"
    if outcome.messages == 0:
        return "✅ 没有待提交的记录，本地记录已经全部同步过了。"
    return (
        f"✅ 同步完成：{outcome.flushed}/{outcome.messages} 条记录，"
        f"{outcome.batches} 批，{outcome.documents} 个文档。"
    )


class MemoryService:
    """插件的记忆服务：持有 HTTP 客户端、同步器与定时任务。"""

    def __init__(self, config: HindsightConfig, store, log) -> None:
        """初始化。

        Args:
            config: 解析好的插件配置。
            store: 待提交记录的来源，即 ``SqlModelManager``。
            log: 插件专属日志对象。
        """
        self._config = config
        self._log = log
        # 一个进程一个连接池：retain 与 recall 共用，避免每次请求重新握手。
        self._client = HindsightClient.from_config(
            config.host, config.port, config.token
        )
        self._sync = HindsightSync(
            store,
            self._client,
            bank_id=config.bank_id,
            max_chars=config.batch_chars,
            log=log,
        )
        self._daily = parse_daily_time(self._config.daily_time)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动每日补投任务。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._daily_loop())
        self._log.info(
            f"Hindsight 记忆服务已启动：{self._client.base_url} "
            f"bank={self._config.bank_id} 每批≤{self._config.batch_chars} 字"
            f"{'（已配置 Token）' if self._client.token_present else '（未配置 Token）'}"
        )

    async def stop(self) -> None:
        """取消定时任务并关闭连接池。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # 主动取消不是异常，吞掉即可。
                pass
            self._task = None
        await self._client.aclose()

    async def _daily_loop(self) -> None:
        """每天在配置的时刻补投一次未提交的记录。"""
        while True:
            delay = seconds_until(self._daily)
            # 防御：配置里若写了非法时刻导致等待过久，也不该整轮静默。
            await asyncio.sleep(max(_MIN_SLEEP, delay))
            await self.sync_once(trigger="每日补投")

    async def sync_once(self, *, trigger: str = "手动") -> SyncOutcome:
        """执行一轮投递，失败只记日志不抛。

        Args:
            trigger: 触发来源，仅用于日志，便于区分定时补投与手动触发。

        Returns:
            本轮的结果摘要。调用方（例如手动指令）可以据此告诉用户投了多少条；
            失败时 ``error`` 非空而不抛异常——投递失败不该让指令本身报错。
        """
        outcome = await self._sync.run()
        if not outcome.ok:
            self._log.warning(f"{trigger}同步中断：{outcome.error}")
            return outcome
        if outcome.messages == 0:
            self._log.debug(f"{trigger}：没有待提交的记录")
            return outcome
        self._log.info(
            f"{trigger}同步完成：{outcome.flushed}/{outcome.messages} 条，"
            f"{outcome.batches} 批，{outcome.documents} 个文档"
        )
        return outcome

    async def recall_for(self, text: str) -> str:
        """按用户这一轮的消息召回记忆文本。

        Args:
            text: 用户消息正文。

        Returns:
            可注入系统提示的文本；没有记忆时为空串。
        """
        return await recall_memories(
            self._client,
            self._config.bank_id,
            text,
            self._config.recall_quantity,
            self._log,
        )
