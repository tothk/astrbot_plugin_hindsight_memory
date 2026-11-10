"""SqlModelManager 里同步层用到的两个方法：list_unflushed / mark_flushed。

用真实 sqlite（临时目录里的库），不 mock 数据库行为——这两个方法的语义
（按 id 升序、只取未提交、批量更新）正是要靠真实 SQL 才能验证的。
"""

from astrbot_plugin_hindsight_memory.db.models.models import RawMessage


def make_raw(*, text: str = "今天有点累", flushed: int = 0) -> RawMessage:
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
        sql.insert_raw(make_raw(text="已提交", flushed=1))
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


class TestRoundTripWithSync:
    """插入 → 取未提交 → 标记，走完一整圈。"""

    def test_pipeline(self, sql) -> None:
        for text in ("一", "二"):
            sql.insert_raw(make_raw(text=text))
        pending = sql.list_unflushed()
        assert len(pending) == 2
        sql.mark_flushed([row.id for row in pending])
        assert sql.list_unflushed() == []
