"""MemoryService 的「攒够字数就提前投递」触发逻辑。

这里只测触发条件与后台任务的节流，不测投递本身——投递已经由
test_hindsight_sync.py 覆盖。因此把 ``service._sync`` 换成假对象，
避免测试真的去连 Hindsight。
"""

import asyncio

from astrbot_plugin_hindsight_memory.core.memory_service import MemoryService
from astrbot_plugin_hindsight_memory.hindsight.plugin_config import HindsightConfig
from astrbot_plugin_hindsight_memory.hindsight.sync import SyncOutcome


class FakeStore:
    """同步层只要求两个方法，触发逻辑用不到它们，留空实现即可。"""

    def list_unflushed(self, limit: int):
        return []

    def mark_flushed(self, ids) -> int:
        return 0


class FakeSync:
    """记录被调了几次的假同步器。"""

    def __init__(self, outcome: SyncOutcome | None = None) -> None:
        self.outcome = outcome or SyncOutcome(
            messages=2, batches=1, documents=1, flushed=2
        )
        self.calls = 0

    async def run(self) -> SyncOutcome:
        self.calls += 1
        return self.outcome


def make_service(log, *, batch_chars: int = 10) -> tuple[MemoryService, FakeSync]:
    """造一个只关心阈值的服务对象。"""
    service = MemoryService(HindsightConfig(batch_chars=batch_chars), FakeStore(), log)
    sync = FakeSync()
    service._sync = sync
    return service, sync


class TestNoteSaved:
    async def test_below_threshold_does_not_trigger(self, log):
        service, sync = make_service(log, batch_chars=10)

        service.note_saved(4)
        service.note_saved(4)

        assert service._sync_task is None
        assert sync.calls == 0

    async def test_reaching_threshold_triggers_once(self, log):
        service, sync = make_service(log, batch_chars=10)

        service.note_saved(4)
        service.note_saved(6)  # 累计正好 10，达到阈值

        assert service._sync_task is not None
        await service._sync_task
        assert sync.calls == 1

    async def test_counter_resets_after_trigger(self, log):
        # 计数器不清零的话，下一批要再攒一整个阈值才能触发，
        # 那些零头就被拖到每日补投了。
        service, sync = make_service(log, batch_chars=10)

        service.note_saved(10)

        assert service._pending_chars == 0
        await service._sync_task
        assert sync.calls == 1

    async def test_no_second_task_while_one_is_running(self, log):
        # 并发投递会读到同一批 flushed=0 的记录，两边都提交 → 远端重复。
        service, sync = make_service(log, batch_chars=10)

        service.note_saved(10)
        service.note_saved(10)

        assert any("仍在进行" in msg for msg in log.text_of("debug"))
        await service._sync_task
        assert sync.calls == 1

    async def test_negative_length_is_clamped(self, log):
        service, _sync = make_service(log, batch_chars=10)

        service.note_saved(-5)

        assert service._pending_chars == 0

    def test_no_running_loop_is_silent(self, log):
        # 后台任务必须在事件循环里起；没有循环时不该抛异常，
        # 记录还在库里，每日补投会兜住。
        service, sync = make_service(log, batch_chars=1)

        service.note_saved(100)

        assert service._sync_task is None
        assert any("没有运行中的事件循环" in msg for msg in log.text_of("debug"))
        assert sync.calls == 0

    async def test_stop_cancels_background_task(self, log):
        service, _sync = make_service(log, batch_chars=1)
        # 换成一个永远不返回的同步器，模拟「投递正在进行」。
        started = asyncio.Event()

        class SlowSync:
            async def run(self):
                started.set()
                await asyncio.Event().wait()

        service._sync = SlowSync()
        service.note_saved(1)
        await started.wait()

        await service.stop()  # 不应抛 CancelledError

        assert service._sync_task is None
