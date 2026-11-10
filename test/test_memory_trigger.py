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


class ConcurrencyProbe:
    """记录每次 run() 的进出，用来判断两轮是否重叠。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.running = 0
        self.overlapped = False

    async def run(self) -> SyncOutcome:
        index = len([e for e in self.events if e[0] == "enter"])
        self.events.append(("enter", index))
        self.running += 1
        if self.running > 1:
            self.overlapped = True
        # 让出两次事件循环：如果两轮能交错，这里必然被插进来。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.running -= 1
        self.events.append(("exit", index))
        return SyncOutcome(messages=1, batches=1, documents=1, flushed=1)


class TestSyncMutualExclusion:
    """三路触发必须串行，否则会各读一份快照、把同一段内容投两遍。

    后台触发那条路原本由 ``_sync_task`` 挡住，但每日补投与手动指令都直接
    调 ``sync_once()``，挡不住。互斥因此下沉到 ``sync_once()`` 本身。
    """

    async def test_concurrent_calls_do_not_overlap(self, log):
        service, _sync = make_service(log)
        probe = ConcurrencyProbe()
        service._sync = probe

        await asyncio.gather(
            service.sync_once(trigger="手动指令"),
            service.sync_once(trigger="每日补投"),
        )

        assert probe.overlapped is False
        # 严丝合缝：先进先出，没有交叉。
        assert probe.events == [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1)]

    async def test_both_callers_still_get_their_own_result(self, log):
        # 串行不等于吞掉一次调用：两个调用方都要拿到结果，
        # 否则手动指令会一直等不到回复。
        service, _sync = make_service(log)
        service._sync = ConcurrencyProbe()

        first, second = await asyncio.gather(
            service.sync_once(trigger="手动指令"),
            service.sync_once(trigger="每日补投"),
        )

        assert first.flushed == 1
        assert second.flushed == 1

    async def test_manual_call_waits_for_background_sync(self, log):
        # 最容易踩的组合：后台正投着，用户又发了 /hindsight_sync。
        service, _sync = make_service(log, batch_chars=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        probe = ConcurrencyProbe()

        class BlockingFirstSync:
            """只挡第一次调用；后续调用直接走完。

            这样若没有互斥锁，手动那轮会立刻跑完、并和仍在等待的首轮
            重叠——「manual.done() 仍为 False」就是锁生效的证据。
            """

            def __init__(self) -> None:
                self.calls = 0

            async def run(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                return await probe.run()

        service._sync = BlockingFirstSync()
        service.note_saved(1)
        await entered.wait()

        manual = asyncio.create_task(service.sync_once(trigger="手动指令"))
        # 手动那轮必须还在等锁，不能已经开始跑。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert probe.events == []
        assert not manual.done()

        release.set()
        await manual
        await service._sync_task

        assert probe.overlapped is False
        assert probe.events == [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1)]

    async def test_lock_is_released_when_a_round_fails(self, log):
        # 失败如果没有释放锁，之后所有触发都会永久卡死。
        service, _sync = make_service(log)

        class BoomSync:
            async def run(self):
                raise RuntimeError("database is locked")

        service._sync = BoomSync()
        outcome = await service.sync_once(trigger="手动指令")
        assert outcome.ok is False

        service._sync = FakeSync()
        retry = await service.sync_once(trigger="手动指令")
        assert retry.ok is True
        assert retry.flushed == 2
