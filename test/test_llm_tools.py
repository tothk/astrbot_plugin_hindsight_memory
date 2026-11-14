"""``proactive_recall`` / ``proactive_retain`` 两个 LLM 工具的行为。

这两个工具的返回值是**模型的输入**，不是用户看到的消息：返回 ``str`` 会被
追加到下一次请求里，返回 ``MessageEventResult`` 则会被框架当成「工具已经
直接回复用户」——记忆原文就变成了回复正文，还顺带终止了本轮 agent 循环。
这个差别在代码里只差一个 return 类型，所以用测试钉住。
"""

import pytest

from astrbot_plugin_hindsight_memory import main as plugin_main
from astrbot_plugin_hindsight_memory.core.recorder import SaveResult
from astrbot_plugin_hindsight_memory.hindsight.sync import SyncOutcome
from astrbot_plugin_hindsight_memory.main import HindsightMemory

MEMORY = "【相关记忆】以下是你们过往对话中与当前话题相关的片段：\n- 她上周加班到很晚"


class FakeLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)


class FakeMemory:
    def __init__(self, memory: str = MEMORY, outcome: SyncOutcome | None = None) -> None:
        self.memory = memory
        self.queries: list[str] = []
        self.sync_calls: list[str] = []
        self.records_calls: list[list] = []
        self.outcome = outcome or SyncOutcome(
            messages=1, batches=1, documents=1, flushed=1
        )
        self.sync_error: Exception | None = None

    async def recall_for(self, text: str) -> str:
        self.queries.append(text)
        return self.memory

    async def sync_once(self, *, trigger: str = "手动") -> SyncOutcome:  # pragma: no cover
        raise AssertionError("主动写入不该扫全库，只投刚写的这几行")

    async def sync_records(self, records, *, trigger: str = "主动写入") -> SyncOutcome:
        self.sync_calls.append(trigger)
        self.records_calls.append(list(records))
        if self.sync_error is not None:
            raise self.sync_error
        return self.outcome


class FakeRecorder:
    """按批量语义返回结果：默认所有行都是新增的。"""

    def __init__(self, *, result: str = "ok", duplicates: int = 0) -> None:
        # result 取 "ok" / "duplicate" / "fail"
        self.result = result
        self.duplicates = duplicates
        self.batches: list[list] = []

    async def save_many(self, rows, notify):
        self.batches.append(list(rows))
        if self.result == "fail":
            return SaveResult(rows=(), duplicates=0, ok=False)
        if self.result == "duplicate":
            return SaveResult(rows=(), duplicates=len(list(rows)), ok=True)
        kept = list(rows)
        if self.duplicates:
            kept = kept[: max(0, len(kept) - self.duplicates)]
        return SaveResult(
            rows=tuple(kept),
            duplicates=len(list(rows)) - len(kept),
            ok=True,
        )


class FakeEvent:
    """工具只用到会话标识与发送者信息。"""

    def __init__(self) -> None:
        self.unified_msg_origin = "qq_official:FriendMessage:u1"
        self._sender_id = "10001"
        self._sender_name = "小满"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name


class FakeSelf:
    def __init__(self, memory: FakeMemory, recorder: FakeRecorder) -> None:
        self._memory = memory
        self._recorder = recorder
        self.context = object()
        self.logger = FakeLogger()

    async def _notify_save_failed(self, event) -> None:  # pragma: no cover
        raise AssertionError("保存成功时不该提示用户")


def make_self(memory: FakeMemory | None = None, recorder: FakeRecorder | None = None):
    return FakeSelf(memory or FakeMemory(), recorder or FakeRecorder())


@pytest.fixture(autouse=True)
def stub_persona(monkeypatch):
    """人格解析要碰宿主上下文，这里统一替换成固定值。"""

    async def fake_persona(context, event) -> str:
        return "default"

    monkeypatch.setattr(plugin_main, "persona_from_conversation", fake_persona)


class TestProactiveRecall:
    async def test_returns_memory_text_for_the_model(self) -> None:
        fake = make_self()

        result = await HindsightMemory.proactive_recall(fake, FakeEvent(), "她最近怎么样")

        assert result == MEMORY
        # 关键：必须是 str（进 prompt），不能是 MessageEventResult（直接发给用户）。
        assert isinstance(result, str)

    async def test_query_is_passed_through_stripped(self) -> None:
        memory = FakeMemory()
        fake = make_self(memory)

        await HindsightMemory.proactive_recall(fake, FakeEvent(), "  她最近怎么样  ")

        assert memory.queries == ["她最近怎么样"]

    async def test_empty_query_does_not_hit_the_service(self) -> None:
        memory = FakeMemory()
        fake = make_self(memory)

        result = await HindsightMemory.proactive_recall(fake, FakeEvent(), "   ")

        assert memory.queries == []
        assert "没有提供检索内容" in result

    async def test_miss_is_reported_to_the_model(self) -> None:
        fake = make_self(FakeMemory(memory=""))

        result = await HindsightMemory.proactive_recall(fake, FakeEvent(), "不存在的事")

        assert "没有找到相关的记忆" in result


class TestProactiveRetain:
    async def test_all_lines_are_saved_in_one_batch(self) -> None:
        memory = FakeMemory()
        recorder = FakeRecorder()
        fake = make_self(memory, recorder)

        result = await HindsightMemory.proactive_retain(
            fake, FakeEvent(), ["我叫小满", " 我最近在学吉他 "]
        )

        # 一次批量写，不是两轮：逐条写是 N 次事务，中途失败会留下半批。
        assert len(recorder.batches) == 1
        rows = recorder.batches[0]
        assert [raw.raw_message for raw in rows] == ["我叫小满", "我最近在学吉他"]
        # 同一批的时间戳与 document_id 必须一致。
        assert rows[0].document_id == rows[1].document_id
        assert rows[0].message_sent_time == rows[1].message_sent_time
        assert memory.sync_calls == ["主动写入"]
        assert "已将 1 条对话写入长期记忆" in result

    async def test_only_the_new_rows_are_submitted(self) -> None:
        # 关键约束：投递的必须就是刚写进去的这几行，不是库里所有积压。
        memory = FakeMemory()
        recorder = FakeRecorder()
        fake = make_self(memory, recorder)

        await HindsightMemory.proactive_retain(fake, FakeEvent(), ["第一句", "第二句"])

        assert len(memory.records_calls) == 1
        submitted = memory.records_calls[0]
        assert [raw.raw_message for raw in submitted] == ["第一句", "第二句"]
        assert submitted == list(recorder.batches[0])

    async def test_saved_rows_carry_sender_and_persona(self) -> None:
        recorder = FakeRecorder()
        fake = make_self(recorder=recorder)

        await HindsightMemory.proactive_retain(fake, FakeEvent(), ["我叫小满"])

        raw = recorder.batches[0][0]
        assert raw.role == "user"
        assert raw.sender_id == "10001"
        assert raw.sender_name == "小满"
        assert raw.persona_id == "default"
        assert raw.document_id.endswith(":qq_official:FriendMessage:u1")

    async def test_blank_lines_are_dropped(self) -> None:
        recorder = FakeRecorder()
        fake = make_self(recorder=recorder)

        result = await HindsightMemory.proactive_retain(
            fake, FakeEvent(), ["  ", "有意义的一行", "\n"]
        )

        assert [raw.raw_message for raw in recorder.batches[0]] == ["有意义的一行"]
        assert "已将" in result

    async def test_all_blank_means_nothing_written(self) -> None:
        memory = FakeMemory()
        recorder = FakeRecorder()
        fake = make_self(memory, recorder)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["  ", ""])

        assert recorder.batches == []
        assert memory.sync_calls == []
        assert "没有提供要写入的内容" in result

    async def test_save_failure_is_reported_truthfully(self) -> None:
        memory = FakeMemory()
        recorder = FakeRecorder(result="fail")
        fake = make_self(memory, recorder)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        assert "写入失败" in result
        # 一条都没落库就不该投递：空投一轮只会白等，还让日志出现莫名的一轮同步。
        assert memory.sync_calls == []

    async def test_all_duplicates_do_not_report_a_write(self) -> None:
        memory = FakeMemory()
        recorder = FakeRecorder(result="duplicate")
        fake = make_self(memory, recorder)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        # 判重不是故障，也不是「写入成功」——内容早就在库里了。
        assert "重复" in result
        assert "写入失败" not in result
        assert "写入长期记忆" not in result
        assert memory.sync_calls == []

    async def test_partial_duplicates_are_counted(self) -> None:
        memory = FakeMemory()
        recorder = FakeRecorder(duplicates=1)
        fake = make_self(memory, recorder)

        result = await HindsightMemory.proactive_retain(
            fake, FakeEvent(), ["第一句", "第二句"]
        )

        assert "另外 1 条与已有记录重复" in result
        # 只投递真正新增的那一行。
        assert len(memory.records_calls[0]) == 1

    async def test_sync_failure_still_admits_rows_are_local(self) -> None:
        memory = FakeMemory(outcome=SyncOutcome(error="连接超时"))
        fake = make_self(memory)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        assert "已记录 1 条对话到本地" in result
        assert "投递到 Hindsight 失败" in result
        # 本地数据没有丢，所以不能说「写入失败」。
        assert "写入失败" not in result

    async def test_sync_exception_is_reported_not_raised(self) -> None:
        memory = FakeMemory()
        memory.sync_error = RuntimeError("boom")
        fake = make_self(memory)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        assert "出错：boom" in result
        assert fake.logger.errors, "异常应当被记录到日志"

    async def test_pending_operation_is_described_as_queued(self) -> None:
        memory = FakeMemory(outcome=SyncOutcome(messages=1, batches=1, documents=1))
        fake = make_self(memory)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        # 异步投递：flushed=0 只代表已入队，不能说成「已写入」。
        assert "排队" in result

    async def test_nothing_submitted_is_not_reported_as_written(self) -> None:
        # 极端情况：刚写进去的行被别的路径抢先投递，本轮 submit_records 拿到空摘要。
        # 不能冒充「已写入 N 条」——模型会据此对用户撒谎。
        memory = FakeMemory(outcome=SyncOutcome())
        fake = make_self(memory)

        result = await HindsightMemory.proactive_retain(fake, FakeEvent(), ["一句话"])

        assert "已记录 1 条对话" in result
        assert "排队" in result
        assert "写入长期记忆" not in result
