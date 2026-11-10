"""同步链路上的异常兜底。

这一组测试守的是一条契约：``sync_once()`` 不抛异常。三个调用方都不适合处理
异常——每日循环被穿透就永久停摆、手动指令会让 AstrBot 回框架话术、积压触发
是无人 await 的后台任务。所以本地数据库出问题（锁超时、磁盘满、表结构不匹配）
必须转成 ``SyncOutcome.error``。

全部用假对象，不连 sqlite 也不连 Hindsight：这里要验的是「异常往哪儿去」，
不是 SQL 本身对不对。
"""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_hindsight_memory.hindsight.sync import HindsightSync, SyncOutcome

BANK = "test-bank"


class FakeLogger:
    """只要能用就行，断言走 conftest 里的 ``log`` fixture。"""


class BaseStore:
    """同步层要求的所有存储方法，默认都是「一切正常」。"""

    def list_unflushed(self, limit: int = 1000):
        return []

    def mark_flushed(self, ids, value: int = 2) -> int:
        return len(list(ids))

    def mark_enqueued(self, ids, *, operation_id: str, items_count: int = 0) -> int:
        return len(list(ids))

    def pending_operations(self, limit: int = 100):
        return []

    def resolve_operation(
        self, operation_id: str, *, status: str, error: str = "", attempts=None
    ) -> int:
        return 1

    def release_operation(self, operation_id: str) -> int:
        return 0


class OkClient:
    async def get_operation(self, bank_id: str, operation_id: str):
        return {"status": "completed"}


def make_raw(row_id: int):
    """投递路径只需要 id、正文与文档归属，别的字段用占位值。"""
    return SimpleNamespace(
        id=row_id,
        role="user",
        sender_id="10001",
        sender_name="小满",
        raw_message=f"第 {row_id} 条",
        record_date="2026-11-11",
        message_sent_time="2026-11-11T00:30:00+08:00",
        document_id="2026-11-11:umo",
    )


def make_sync(store, client=None, log=None) -> HindsightSync:
    return HindsightSync(
        store,
        client or OkClient(),
        bank_id=BANK,
        max_chars=5000,
        log=log or FakeLogger(),
    )


def make_operation(operation_id: str, *, attempts: int = 0):
    return SimpleNamespace(operation_id=operation_id, attempts=attempts)


class TestStoreFailuresBecomeOutcome:
    async def test_list_unflushed_failure_is_reported(self, log):
        class BrokenStore(BaseStore):
            def list_unflushed(self, limit: int = 1000):
                raise RuntimeError("database is locked")

        outcome = await make_sync(BrokenStore(), log=log).run()

        assert outcome.ok is False
        assert "database is locked" in (outcome.error or "")

    async def test_mark_enqueued_failure_aborts_before_sending(self, log):
        # 凭据没落库就不该发请求：记录还停在待投递，服务端什么都不知道，
        # 下一轮正常重投即可，不会产生第二份内容。
        class BrokenStore(BaseStore):
            def list_unflushed(self, limit: int = 1000):
                return [make_raw(1)]

            def mark_enqueued(self, ids, *, operation_id, items_count=0):
                raise RuntimeError("disk full")

        calls = []

        class CountingClient:
            async def retain(self, bank_id, items, **kwargs):
                calls.append(kwargs.get("operation_id"))
                return SimpleNamespace(success=True, operation_id="op-1")

        outcome = await make_sync(BrokenStore(), CountingClient(), log=log).run()

        assert outcome.ok is False
        assert "disk full" in (outcome.error or "")
        assert outcome.flushed == 0
        # 关键断言：请求根本没发出去
        assert calls == []

    async def test_retain_failure_keeps_rows_enqueued(self, log):
        # 提交失败时不能把记录退回待投递：服务端可能其实收到了，退回会让它
        # 被再投一次。留在「已入队」，下一轮对账查这个 id 就能分清。
        class RecordingStore(BaseStore):
            def __init__(self):
                self.enqueued = []
                self.released = []

            def list_unflushed(self, limit: int = 1000):
                return [make_raw(1)]

            def mark_enqueued(self, ids, *, operation_id, items_count=0):
                self.enqueued.append((list(ids), operation_id))
                return 1

            def release_operation(self, operation_id):
                self.released.append(operation_id)
                return 1

        class DeadClient:
            async def retain(self, bank_id, items, **kwargs):
                from astrbot_plugin_hindsight_memory.hindsight.client import (
                    HindsightConnectionError,
                )

                raise HindsightConnectionError("连接超时")

        store = RecordingStore()
        outcome = await make_sync(store, DeadClient(), log=log).run()

        assert outcome.ok is False
        assert outcome.flushed == 0
        # 凭据在发请求之前就落库了
        assert len(store.enqueued) == 1
        # 而且没有把它退回待投递
        assert store.released == []

    async def test_missing_operation_id_in_response_still_uses_local_id(self, log):
        # 服务端没回 operation_id 时不该丢掉本地这个 id：它是我们对账的唯一
        # 线索，而且请求是我们发的、id 也是我们给的。
        class RecordingStore(BaseStore):
            def __init__(self):
                self.enqueued = []
                self.flushed_marks = []

            def list_unflushed(self, limit: int = 1000):
                return [make_raw(1)]

            def mark_enqueued(self, ids, *, operation_id, items_count=0):
                self.enqueued.append(operation_id)
                return 1

            def mark_flushed(self, ids, value: int = 2):
                self.flushed_marks.append((list(ids), value))
                return 1

        class NoIdClient:
            async def retain(self, bank_id, items, **kwargs):
                return SimpleNamespace(success=True, operation_id="")

        store = RecordingStore()
        outcome = await make_sync(store, NoIdClient(), log=log).run()

        assert outcome.ok is True
        assert outcome.flushed == 1
        # 凭据仍然只有提交前那一个，没有被「按已提交处理」覆盖掉
        assert len(store.enqueued) == 1
        assert store.flushed_marks == []
        assert any("继续使用本地 id" in msg for msg in log.text_of("warning"))

    async def test_reconcile_resolve_failure_does_not_stop_other_batches(self, log):
        # 一个批次写不进去，别的批次仍该对账完成——否则每次都要从头再来。
        class FlakyResolveStore(BaseStore):
            def __init__(self):
                self.resolved = []

            def pending_operations(self, limit: int = 100):
                return [make_operation("op-bad"), make_operation("op-good")]

            def resolve_operation(self, operation_id, *, status, error="", attempts=None):
                if operation_id == "op-bad":
                    raise RuntimeError("database is locked")
                self.resolved.append(operation_id)
                return 1

        store = FlakyResolveStore()

        outcome = await make_sync(store, log=log).run()

        assert store.resolved == ["op-good"]
        assert outcome.completed == 1
        # 对账还能继续，说明这一轮整体是成功的（没有投递内容，error 为空）。
        assert outcome.ok is True

    async def test_release_failure_is_swallowed(self, log):
        # 撤回失败时这批记录会继续停在「已入队」，下轮再试；
        # 但整轮同步不该因此中断。
        class BrokenReleaseStore(BaseStore):
            def pending_operations(self, limit: int = 100):
                return [make_operation("op-gone")]

            def release_operation(self, operation_id: str) -> int:
                raise RuntimeError("database is locked")

        class MissingClient:
            async def get_operation(self, bank_id, operation_id):
                from astrbot_plugin_hindsight_memory.hindsight.client import (
                    HindsightOperationNotFoundError,
                )

                raise HindsightOperationNotFoundError("已清理")

        outcome = await make_sync(BrokenReleaseStore(), MissingClient(), log=log).run()

        assert outcome.released == 0
        assert outcome.ok is True

    async def test_reconcile_pending_read_failure_does_not_block_submit(self, log):
        # 对账读不到批次，投递新内容仍然有意义。
        class BrokenPendingStore(BaseStore):
            def pending_operations(self, limit: int = 100):
                raise RuntimeError("database is locked")

            def list_unflushed(self, limit: int = 1000):
                return [make_raw(1)]

        class QueueClient:
            async def retain(self, bank_id, items, **kwargs):
                return SimpleNamespace(success=True, operation_id="op-1")

        outcome = await make_sync(BrokenPendingStore(), QueueClient(), log=log).run()

        assert outcome.flushed == 1
        assert outcome.ok is True


class TestSyncOnceNeverRaises:
    async def test_run_exception_becomes_outcome(self, log):
        from astrbot_plugin_hindsight_memory.core.memory_service import MemoryService
        from astrbot_plugin_hindsight_memory.hindsight.plugin_config import (
            HindsightConfig,
        )

        class ExplodingSync:
            async def run(self):
                raise RuntimeError("database is locked")

        service = MemoryService(HindsightConfig(), BaseStore(), log)
        service._sync = ExplodingSync()

        outcome = await service.sync_once(trigger="手动指令")

        assert isinstance(outcome, SyncOutcome)
        assert outcome.ok is False
        assert "database is locked" in (outcome.error or "")


class TestDailyLoopSurvives:
    async def test_one_exception_does_not_kill_the_loop(self, monkeypatch, log):
        """异常穿透一次就永久停摆——这是最贵的失败模式，必须挡住。

        这里直接让 ``sync_once`` 抛，绕开它自己那层兜底：万一将来有人在那层
        里加了新的存储调用而漏了处理，循环本身仍要活着。
        """
        from astrbot_plugin_hindsight_memory.core import memory_service as module

        # 把等待压到零：这里要验的是循环能不能活过异常，不是等多久。
        monkeypatch.setattr(module, "_MIN_SLEEP", 0)
        monkeypatch.setattr(module, "seconds_until", lambda _target: 0)

        service = module.MemoryService(module.HindsightConfig(), BaseStore(), log)
        recovered = asyncio.Event()
        calls = {"count": 0}

        async def flaky_sync_once(*, trigger: str = ""):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("database is locked")
            recovered.set()
            return SyncOutcome()

        monkeypatch.setattr(service, "sync_once", flaky_sync_once)

        task = asyncio.create_task(service._daily_loop())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls["count"] >= 2
        assert any("每日补投异常" in msg for msg in log.text_of("error"))

    async def test_sync_run_exception_is_absorbed_by_sync_once(self, log):
        """第一层兜底：run() 抛出来的异常不该走到循环层。"""
        from astrbot_plugin_hindsight_memory.core import memory_service as module

        class ExplodingSync:
            async def run(self):
                raise RuntimeError("database is locked")

        service = module.MemoryService(module.HindsightConfig(), BaseStore(), log)
        service._sync = ExplodingSync()

        outcome = await service.sync_once(trigger="每日补投")

        assert outcome.ok is False
        assert any("每日补投同步异常" in msg for msg in log.text_of("error"))

    async def test_stop_does_not_reraise_dead_task(self, log):
        """任务是 await 出来的：它若因意外死掉，异常会重新抛进 terminate()。"""
        from astrbot_plugin_hindsight_memory.core.memory_service import MemoryService
        from astrbot_plugin_hindsight_memory.hindsight.plugin_config import (
            HindsightConfig,
        )

        service = MemoryService(HindsightConfig(), BaseStore(), log)

        async def boom():
            raise RuntimeError("unexpected")

        service._task = asyncio.create_task(boom())
        await asyncio.sleep(0)  # 让任务跑完并留下异常

        await service.stop()  # 不应抛

        assert service._task is None
        assert any("每日补投任务结束时异常" in msg for msg in log.text_of("error"))
