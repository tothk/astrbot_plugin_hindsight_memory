"""SqlModelManager 里投递相关的读写方法。

用真实 sqlite（临时目录里的库），不 mock 数据库行为——这些方法的语义
（按 id 升序、只取待投递、三态转换、批次区间回写）正是要靠真实 SQL 才能验证的。
"""

from astrbot_plugin_hindsight_memory.db.models.models import (
    FLUSHED_DONE,
    FLUSHED_ENQUEUED,
    FLUSHED_PENDING,
    RawMessage,
)
from astrbot_plugin_hindsight_memory.hindsight.client import RetainResult


def make_raw(*, text: str = "今天有点累", flushed: int = FLUSHED_PENDING) -> RawMessage:
    """造一条可插入的记录。content_hash 必须唯一，否则会撞唯一索引。"""
    return RawMessage(
        record_date="2026-11-10",
        document_id=f"2026-11-10:{text}",  # 用 text 保证 document_id 与 hash 都不重复
        sender_id="10001",
        persona_id="",
        role="user",
        raw_message=text,
        message_sent_time="2026-11-10T10:30:00+08:00",
        content_hash=f"hash-{text}",
        flushed=flushed,
    )


class TestListUnflushed:
    """取出待提交记录。"""

    def test_empty_database(self, sql) -> None:
        assert sql.list_unflushed() == []

    def test_returns_only_unflushed(self, sql) -> None:
        sql.insert_raw(make_raw(text="已提交", flushed=FLUSHED_DONE))
        sql.insert_raw(make_raw(text="待提交"))
        rows = sql.list_unflushed()
        assert [row.raw_message for row in rows] == ["待提交"]

    def test_skips_enqueued(self, sql) -> None:
        """已入队但未确认的记录不能再取出来：再投一次会在文档末尾重复追加。"""
        sql.insert_raw(make_raw(text="已入队", flushed=FLUSHED_ENQUEUED))
        sql.insert_raw(make_raw(text="待提交"))
        rows = sql.list_unflushed()
        assert [row.raw_message for row in rows] == ["待提交"]

    def test_ordered_by_id(self, sql) -> None:
        for text in ("一", "二", "三"):
            sql.insert_raw(make_raw(text=text))
        rows = sql.list_unflushed()
        assert [row.raw_message for row in rows] == ["一", "二", "三"]
        assert [row.id for row in rows] == sorted(row.id for row in rows)

    def test_respects_limit(self, sql) -> None:
        for text in ("一", "二", "三"):
            sql.insert_raw(make_raw(text=text))
        assert len(sql.list_unflushed(limit=2)) == 2


class TestMarkFlushed:
    """标记已提交。"""

    def test_marks_given_ids(self, sql) -> None:
        first = sql.insert_raw(make_raw(text="一"))
        sql.insert_raw(make_raw(text="二"))
        assert sql.mark_flushed([first.id]) == 1
        assert [row.raw_message for row in sql.list_unflushed()] == ["二"]

    def test_empty_ids_is_noop(self, sql) -> None:
        sql.insert_raw(make_raw(text="一"))
        assert sql.mark_flushed([]) == 0
        assert len(sql.list_unflushed()) == 1

    def test_unknown_id_updates_nothing(self, sql) -> None:
        sql.insert_raw(make_raw(text="一"))
        assert sql.mark_flushed([99999]) == 0
        assert len(sql.list_unflushed()) == 1

    def test_idempotent(self, sql) -> None:
        """重复标记不会报错，也不会把已标记的行再算一次。"""
        row = sql.insert_raw(make_raw(text="一"))
        assert sql.mark_flushed([row.id]) == 1
        assert sql.mark_flushed([row.id]) == 1
        assert sql.list_unflushed() == []

    def test_can_mark_enqueued(self, sql) -> None:
        """value 参数可以直接把记录标成「已入队」。"""
        row = sql.insert_raw(make_raw(text="一"))
        assert sql.mark_flushed([row.id], FLUSHED_ENQUEUED) == 1
        assert sql.list_unflushed() == []


class TestCountByFlushed:
    """三态计数，用于日志与状态展示。"""

    def test_counts_each_state(self, sql) -> None:
        sql.insert_raw(make_raw(text="一"))
        sql.insert_raw(make_raw(text="二"))
        sql.insert_raw(make_raw(text="三", flushed=FLUSHED_ENQUEUED))
        sql.insert_raw(make_raw(text="四", flushed=FLUSHED_DONE))
        assert sql.count_by_flushed() == {0: 2, 1: 1, 2: 1}

    def test_empty_database(self, sql) -> None:
        assert sql.count_by_flushed() == {}


class TestEnqueueAndResolve:
    """入队 → 对账 → 完成／失败／撤回 的完整链路。"""

    def test_mark_enqueued_writes_batch_record(self, sql) -> None:
        first = sql.insert_raw(make_raw(text="一"))
        second = sql.insert_raw(make_raw(text="二"))
        updated = sql.mark_enqueued(
            [first.id, second.id], operation_id="op-1", items_count=1
        )
        assert updated == 2
        # 记录变成「已入队」，不再出现在待投递里
        assert sql.list_unflushed() == []
        # 批次凭据被写了下来，巡检才知道该问哪个 operation
        pending = sql.pending_operations()
        assert len(pending) == 1
        assert pending[0].operation_id == "op-1"
        assert pending[0].first_row_id == first.id
        assert pending[0].last_row_id == second.id

    def test_mark_enqueued_empty_ids_is_noop(self, sql) -> None:
        assert sql.mark_enqueued([], operation_id="op-1") == 0
        assert sql.pending_operations() == []

    def test_resolve_completed_marks_rows_done(self, sql) -> None:
        first = sql.insert_raw(make_raw(text="一"))
        second = sql.insert_raw(make_raw(text="二"))
        sql.mark_enqueued([first.id, second.id], operation_id="op-1")
        assert sql.resolve_operation("op-1", status="completed") == 2
        # 批次记录已删除（对账完成就不必再查）
        assert sql.pending_operations() == []
        assert sql.count_by_flushed() == {FLUSHED_DONE: 2}

    def test_resolve_completed_leaves_pending_rows_alone(self, sql) -> None:
        """区间里后来补插的行仍停在 PENDING，不能被顺手标成完成。"""
        first = sql.insert_raw(make_raw(text="一"))
        last = sql.insert_raw(make_raw(text="二"))
        sql.mark_enqueued([first.id, last.id], operation_id="op-1")
        gap = sql.insert_raw(make_raw(text="夹在中间的新行"))
        sql.resolve_operation("op-1", status="completed")
        assert gap.id is not None
        pending = sql.list_unflushed()
        assert [row.raw_message for row in pending] == ["夹在中间的新行"]

    def test_resolve_failed_keeps_rows_enqueued(self, sql) -> None:
        row = sql.insert_raw(make_raw(text="一"))
        sql.mark_enqueued([row.id], operation_id="op-1")
        assert sql.resolve_operation("op-1", status="failed", error="boom", attempts=1) == 1
        pending = sql.pending_operations()
        assert len(pending) == 1
        assert pending[0].status == "failed"
        assert pending[0].error == "boom"
        assert pending[0].attempts == 1
        # 记录仍停在「已入队」，等 retry 或最终撤回
        assert sql.count_by_flushed() == {FLUSHED_ENQUEUED: 1}

    def test_resolve_unknown_operation_returns_zero(self, sql) -> None:
        assert sql.resolve_operation("nope", status="completed") == 0

    def test_release_operation_returns_rows_to_pending(self, sql) -> None:
        first = sql.insert_raw(make_raw(text="一"))
        second = sql.insert_raw(make_raw(text="二"))
        sql.mark_enqueued([first.id, second.id], operation_id="op-1")
        sql.resolve_operation("op-1", status="failed", attempts=3)
        assert sql.release_operation("op-1") == 2
        # 批次凭据已删除，记录退回待投递
        assert sql.pending_operations() == []
        assert len(sql.list_unflushed()) == 2

    def test_release_unknown_operation_returns_zero(self, sql) -> None:
        assert sql.release_operation("nope") == 0

    def test_pending_operations_ordered_by_id(self, sql) -> None:
        for text in ("一", "二", "三"):
            row = sql.insert_raw(make_raw(text=text))
            sql.mark_enqueued([row.id], operation_id=f"op-{text}")
        assert [op.operation_id for op in sql.pending_operations()] == [
            "op-一",
            "op-二",
            "op-三",
        ]


class TestRoundTripWithSync:
    """插入 → 取未提交 → 标记，走完一整圈。"""

    def test_pipeline(self, sql) -> None:
        for text in ("一", "二"):
            sql.insert_raw(make_raw(text=text))
        pending = sql.list_unflushed()
        assert len(pending) == 2
        sql.mark_flushed([row.id for row in pending])
        assert sql.list_unflushed() == []


class TestCrashRecovery:
    """崩在「凭据已落库、请求没到」的那个窗口里，下一轮能自愈。"""

    async def test_lost_submission_is_released_then_retried(self, sql, log) -> None:
        from sqlmodel import Session, select

        from astrbot_plugin_hindsight_memory.db.models.models import SyncOperation
        from astrbot_plugin_hindsight_memory.hindsight.client import (
            HindsightConnectionError,
            HindsightOperationNotFoundError,
        )
        from astrbot_plugin_hindsight_memory.hindsight.sync import HindsightSync
        class FlakyClient:
            """第一次提交「丢包」，之后恢复正常。"""

            def __init__(self) -> None:
                self.operation_ids: list[str] = []
                self.submitted: set[str] = set()

            async def retain(self, bank_id, items, *, is_async=True, operation_id=None, **_):
                self.operation_ids.append(operation_id)
                if len(self.operation_ids) == 1:
                    # 请求发出后进程就没了：服务端从没收到过这个 id
                    raise HindsightConnectionError("进程被卸载")
                self.submitted.add(operation_id)
                return RetainResult(
                    success=True, bank_id=bank_id, items_count=len(items),
                    is_async=is_async, usage={}, operation_id=operation_id,
                )

            async def get_operation(self, bank_id, operation_id):
                if operation_id in self.submitted:
                    return {"status": "completed"}
                raise HindsightOperationNotFoundError("查无此操作")

            async def retry_operation(self, bank_id, operation_id):
                raise AssertionError("操作不存在，retry 无从谈起")

        sql.insert_raw(make_raw(text="崩前的记录"))
        client = FlakyClient()
        sync = HindsightSync(sql, client, bank_id="dev", max_chars=10_000, log=log)

        first = await sync.run()
        assert first.ok is False
        assert first.flushed == 0
        # 凭据已落库、记录停在「已入队」——这正是需要自愈的状态
        assert sql.count_by_flushed() == {FLUSHED_ENQUEUED: 1}
        lost_id = client.operation_ids[0]

        # 下一轮：对账查无此 id → 退回待投递 → 用新 id 重投成功
        second = await sync.run()
        assert second.released == 1
        assert sql.count_by_flushed() == {FLUSHED_ENQUEUED: 1}
        # 重投用的是新 id，旧 id 不会被复用
        assert client.operation_ids[1] != lost_id
        assert len(client.operation_ids) == 2
        # 旧凭据记录已被清掉，只剩新的一条
        with Session(sql._engine) as session:
            records = session.exec(select(SyncOperation)).all()
        assert [r.operation_id for r in records] == [client.operation_ids[1]]
