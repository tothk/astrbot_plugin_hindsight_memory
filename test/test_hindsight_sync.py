"""hindsight/sync.py 的单元测试。

分两部分：装配层的纯函数（不碰网络、不碰数据库）与投递层的编排
（用假 store + 假 client，仍然不碰真实服务）。
"""

from dataclasses import dataclass

import pytest

from astrbot_plugin_hindsight_memory.db.models.models import RawMessage
from astrbot_plugin_hindsight_memory.hindsight.client import (
    HindsightConnectionError,
    RetainResult,
)
from astrbot_plugin_hindsight_memory.hindsight.sync import (
    HindsightSync,
    build_items,
    render_line,
    split_batches,
)

# 固定时间，避免断言里出现"当前时刻"这种不确定值。
T1 = "2026-11-10T10:30:00+08:00"
T2 = "2026-11-10T10:30:05+08:00"


def make_raw(
    *,
    id: int | None = None,
    role: str = "user",
    sender_id: str = "10001",
    text: str = "今天有点累",
    document_id: str = "2026-11-10:aiocqhttp:FriendMessage:10001",
    sent_time: str = T1,
    flushed: int = 0,
) -> RawMessage:
    """造一条记录，只覆盖测试关心的字段。"""
    return RawMessage(
        id=id,
        record_date=document_id.split(":")[0],
        document_id=document_id,
        sender_id=sender_id,
        persona_id="",
        role=role,
        raw_message=text,
        message_sent_time=sent_time,
        content_hash=f"h-{id}-{role}-{text}",
        flushed=flushed,
    )


class TestRenderLine:
    """一条记录怎么变成对话文本里的一行。"""

    def test_basic_shape(self) -> None:
        """标准形状是 ``发送者 (时间): 正文``。"""
        assert render_line(make_raw()) == f"10001 ({T1}): 今天有点累"

    def test_llm_row_uses_bot_as_sender(self) -> None:
        raw = make_raw(role="llm", sender_id="bot-42", text="那就早点休息", sent_time=T2)
        assert render_line(raw) == f"bot-42 ({T2}): 那就早点休息"

    def test_multiline_text_is_collapsed(self) -> None:
        """正文里的换行必须压平，否则第二行起就没有说话人了。"""
        raw = make_raw(text="第一行\n第二行\r\n\n第三行")
        assert render_line(raw) == f"10001 ({T1}): 第一行 第二行 第三行"

    def test_empty_text_keeps_prefix_only(self) -> None:
        """正文为空时保留前缀，不留一个尾部空格。"""
        assert render_line(make_raw(text="   ")) == f"10001 ({T1}):"


class TestSplitBatches:
    """按字符上限切批。"""

    def test_empty_input(self) -> None:
        assert split_batches([], 100) == []

    def test_single_batch_when_under_limit(self) -> None:
        messages = [make_raw(id=i, text="短") for i in range(3)]
        batches = split_batches(messages, 10_000)
        assert len(batches) == 1
        assert batches[0] == messages

    def test_splits_when_over_limit(self) -> None:
        """每行约 40 字符，上限 100 时大约每批两三条。"""
        messages = [make_raw(id=i, text="x" * 20) for i in range(6)]
        batches = split_batches(messages, 100)
        assert len(batches) > 1
        # 所有记录恰好出现一次且顺序不变。
        assert [raw.id for batch in batches for raw in batch] == list(range(6))

    def test_oversize_single_record_is_not_dropped(self) -> None:
        """单条就超限时不能丢，也不能陷入空批死循环。"""
        messages = [make_raw(id=1, text="y" * 500)]
        batches = split_batches(messages, 100)
        assert len(batches) == 1
        assert [raw.id for raw in batches[0]] == [1]

    def test_oversize_record_does_not_block_following(self) -> None:
        """超限的一条自成一批，后面的记录正常成批。"""
        messages = [make_raw(id=1, text="y" * 500), make_raw(id=2, text="短")]
        batches = split_batches(messages, 100)
        assert [raw.id for batch in batches for raw in batch] == [1, 2]
        # 第一条独占一批（因为加入它时批为空，紧随其后的第二条会触发封批）。
        assert [raw.id for raw in batches[0]] == [1]

    def test_batches_do_not_exceed_limit_when_possible(self) -> None:
        """非超限场景下，每批渲染后拼接的长度不超过上限。"""
        messages = [make_raw(id=i, text="z" * 30) for i in range(8)]
        for batch in split_batches(messages, 150):
            if len(batch) == 1:
                continue
            total = sum(len(render_line(raw)) + 1 for raw in batch)
            assert total <= 150

    def test_zero_limit_rejected(self) -> None:
        """上限为 0 会让累加永远失败，直接报错比死循环好。"""
        with pytest.raises(ValueError):
            split_batches([make_raw()], 0)


class TestBuildItems:
    """一批记录怎么变成 retain 的 items。"""

    def test_groups_by_document_id(self) -> None:
        """同一文档的记录合并成一个 item，不同文档各一个。"""
        batch = [
            make_raw(id=1, text="A", document_id="d1"),
            make_raw(id=2, text="B", document_id="d2"),
            make_raw(id=3, text="C", document_id="d1"),
        ]
        items = build_items(batch)
        assert len(items) == 2
        assert items[0]["document_id"] == "d1"
        # d1 的两条按出现顺序拼接在同一段正文里。
        lines = items[0]["content"].split("\n")
        assert len(lines) == 2
        assert lines[0].endswith(": A")
        assert lines[1].endswith(": C")
        assert items[1]["document_id"] == "d2"

    def test_update_mode_is_append(self) -> None:
        """重复提交同一 document_id 要追加而不是重建。"""
        items = build_items([make_raw()])
        assert items[0]["update_mode"] == "append"

    def test_timestamp_is_first_record_of_document(self) -> None:
        """timestamp 取文档内最早那条，避免被摄取时刻顶替。"""
        batch = [
            make_raw(id=1, sent_time=T1, document_id="d1"),
            make_raw(id=2, sent_time=T2, document_id="d1"),
        ]
        items = build_items(batch)
        assert items[0]["timestamp"] == T1

    def test_empty_batch(self) -> None:
        assert build_items([]) == []


@dataclass
class FakeStore:
    """只实现同步层需要的两个方法，用来替代 SqlModelManager。"""

    pending: list[RawMessage]
    marked: list[int] | None = None

    def __post_init__(self) -> None:
        self.marked = []

    def list_unflushed(self, limit: int) -> list[RawMessage]:
        return self.pending[:limit]

    def mark_flushed(self, ids) -> int:
        self.marked.extend(ids)
        return len(ids)


class FakeClient:
    """记录每次 retain 调用的假客户端。"""

    def __init__(self, *, success: bool = True, error: Exception | None = None) -> None:
        self.success = success
        self.error = error
        self.calls: list[tuple[str, list[dict]]] = []
        # 记录每次调用用的是同步还是异步模式：这层必须钉住同步语义，
        # 异步改造之前若被悄悄改成 True，200 就只代表「已排队」。
        self.async_modes: list[bool] = []

    async def retain(
        self, bank_id: str, items: list[dict], *, is_async: bool = True, **_: object
    ) -> RetainResult:
        self.calls.append((bank_id, items))
        self.async_modes.append(is_async)
        if self.error is not None:
            raise self.error
        return RetainResult(
            success=self.success,
            bank_id=bank_id,
            items_count=len(items),
            is_async=is_async,
            usage={},
        )


class TestHindsightSyncRun:
    """投递层的编排流程。"""

    async def test_no_pending_does_nothing(self, log) -> None:
        store = FakeStore([])
        client = FakeClient()
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=10_000, log=log
        ).run()
        assert outcome.ok is True
        assert outcome.messages == 0
        assert client.calls == []
        assert store.marked == []

    async def test_success_marks_flushed(self, log) -> None:
        store = FakeStore([make_raw(id=1), make_raw(id=2, text="第二条")])
        client = FakeClient()
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=10_000, log=log
        ).run()
        assert outcome.ok is True
        assert outcome.flushed == 2
        assert store.marked == [1, 2]
        assert len(client.calls) == 1
        assert client.calls[0][0] == "dev"

    async def test_error_does_not_mark_flushed(self, log) -> None:
        """提交失败不能标记，否则这批内容就永远丢了。"""
        store = FakeStore([make_raw(id=1)])
        client = FakeClient(error=HindsightConnectionError("连不上"))
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=10_000, log=log
        ).run()
        assert outcome.ok is False
        assert outcome.flushed == 0
        assert store.marked == []
        assert log.text_of("error")

    async def test_success_false_is_treated_as_failure(self, log) -> None:
        """服务端回 200 但 success=false，同样不能标记。"""
        store = FakeStore([make_raw(id=1)])
        client = FakeClient(success=False)
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=10_000, log=log
        ).run()
        assert outcome.ok is False
        assert store.marked == []

    async def test_stops_at_first_failing_batch(self, log) -> None:
        """首批失败后不再尝试后续批次，避免把错误日志刷满。"""
        messages = [make_raw(id=i, text="x" * 400) for i in range(4)]
        store = FakeStore(messages)
        client = FakeClient(error=HindsightConnectionError("连不上"))
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=100, log=log
        ).run()
        assert outcome.ok is False
        assert len(client.calls) == 1
        assert store.marked == []

    async def test_multiple_batches_all_marked(self, log) -> None:
        """多批全部成功时，每批各自标记。"""
        messages = [make_raw(id=i, text="x" * 100) for i in range(5)]
        store = FakeStore(messages)
        client = FakeClient()
        outcome = await HindsightSync(
            store, client, bank_id="dev", max_chars=150, log=log
        ).run()
        assert outcome.ok is True
        assert len(client.calls) > 1
        assert store.marked == [0, 1, 2, 3, 4]
        assert outcome.flushed == 5
