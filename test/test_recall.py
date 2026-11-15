"""core/recall.py 的单元测试。

重点在「库不存在」这条分支——它是首次安装必走的路，出错时表现是
「悄悄没有记忆」，最难被发现。现在的语义是：热路径只发一次请求，
建库丢到后台，本轮按「没有记忆」处理。
"""

import asyncio

import pytest

from astrbot_plugin_hindsight_memory.core.recall import (
    _DEFAULT_TIMEOUT,
    _REQUEST_TIMEOUT,
    _recall_or_create_bank,
    recall_memories,
)
from astrbot_plugin_hindsight_memory.hindsight.client import (
    HindsightBankNotFoundError,
    HindsightConnectionError,
)


class Log:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def debug(self, message):
        self.records.append(("debug", message))

    def info(self, message):
        self.records.append(("info", message))

    def warning(self, message):
        self.records.append(("warning", message))

    def error(self, message):
        self.records.append(("error", message))

    def text_of(self, level):
        return [m for lvl, m in self.records if lvl == level]


class FakeClient:
    """按预设剧本作答的假客户端。

    用剧本而不是 Mock：要断言的是「调用顺序与次数」，
    ``recall`` 被调用几次、建库发生在第几次之后，都是行为的一部分。
    """

    def __init__(self, *, responses=None, create_ok=True):
        self._responses = list(responses or [{"results": []}])
        self._create_ok = create_ok
        self.calls: list[tuple] = []
        # 与 calls 分开记：calls 断言的是顺序与次数，keywords 断言的是参数。
        self.keywords: list[dict] = []

    async def recall(self, bank_id, query, **kwargs):
        self.calls.append(("recall", bank_id, query))
        self.keywords.append(dict(kwargs))
        item = self._responses.pop(0) if self._responses else {"results": []}
        if isinstance(item, Exception):
            raise item
        return item

    async def ensure_bank(self, bank_id):
        self.calls.append(("ensure_bank", bank_id))
        if not self._create_ok:
            raise HindsightConnectionError("建库被拒")
        return True


class TestRecallOrCreateBank:
    async def test_normal_recall_does_not_touch_bank(self):
        client = FakeClient()
        log = Log()

        await _recall_or_create_bank(client, "b1", "q", log)

        assert client.calls == [("recall", "b1", "q")]
        assert log.text_of("info") == []

    async def test_missing_bank_spawns_background_creation(self):
        # 库不存在时热路径只发一次请求就返回：库里没有东西，这一轮本来也
        # 召不到记忆，不该为了让建库挤进 3 秒预算而多做几次往返。
        client = FakeClient(
            responses=[HindsightBankNotFoundError("Bank 'b1' not found")]
        )
        log = Log()

        data = await _recall_or_create_bank(client, "b1", "q", log)

        assert data == {"results": []}
        # 热路径上只有一次 recall，没有第二次。
        assert client.calls == [("recall", "b1", "q")]

        # 建库被丢到后台：让出一次事件循环，它就该跑完。
        await asyncio.sleep(0)
        assert client.calls == [("recall", "b1", "q"), ("ensure_bank", "b1")]
        assert any("不存在" in m for m in log.text_of("info"))

    async def test_uses_low_budget(self):
        # 服务端默认 mid 会稳定跑满超时，这里钉死成 low。
        client = FakeClient()

        await _recall_or_create_bank(client, "b1", "q", Log())

        assert client.keywords[0]["budget"] == "low"

    async def test_request_timeout_is_below_outer_timeout(self):
        # 内层 HTTP 超时必须小于外层 wait_for：否则外层先到期，正在飞的
        # 请求被直接取消，日志里只剩一句「超时」，看不出是哪个请求慢。
        client = FakeClient()

        await _recall_or_create_bank(client, "b1", "q", Log())

        request_timeout = client.keywords[0]["timeout"]
        assert request_timeout == pytest.approx(_REQUEST_TIMEOUT)
        assert request_timeout < _DEFAULT_TIMEOUT

    async def test_filters_to_observation_at_server(self):
        # 类型过滤必须交给服务端：它参与检索与重排，客户端筛的话
        # 重排仍要处理全部类型的候选，正是召回超时的根因。
        client = FakeClient()

        await _recall_or_create_bank(client, "b1", "q", Log())

        assert client.keywords[0]["types"] == ["observation"]

    async def test_background_create_failure_does_not_escape(self):
        # 后台任务没人 await，异常逃出去只会变成
        # 「Task exception was never retrieved」噪声。
        client = FakeClient(
            responses=[HindsightBankNotFoundError("not found")], create_ok=False
        )
        log = Log()

        data = await _recall_or_create_bank(client, "b1", "q", log)

        assert data == {"results": []}
        await asyncio.sleep(0)  # 让后台任务执行并吞掉异常
        assert any("后台建库失败" in m for m in log.text_of("warning"))


class TestRecallMemories:
    async def test_returns_formatted_text(self):
        client = FakeClient(
            responses=[{"results": [{"text": "她喜欢猫", "type": "observation"}]}]
        )

        text = await recall_memories(client, "b1", "她喜欢什么", 5, Log())

        assert "她喜欢猫" in text

    async def test_empty_query_short_circuits(self):
        client = FakeClient()

        assert await recall_memories(client, "b1", "   ", 5, Log()) == ""
        assert client.calls == []

    async def test_zero_quantity_short_circuits(self):
        client = FakeClient()

        assert await recall_memories(client, "b1", "q", 0, Log()) == ""
        assert client.calls == []

    async def test_bank_not_found_returns_empty_without_raising(self):
        # 库不存在不该冒到调用方，也不该在本轮重试：返回空结果，
        # 建库由后台去做。
        client = FakeClient(responses=[HindsightBankNotFoundError("not found")])

        text = await recall_memories(client, "b1", "q", 5, Log())

        assert text == ""

    async def test_other_errors_return_empty_string(self):
        client = FakeClient(responses=[HindsightConnectionError("服务没起")])
        log = Log()

        assert await recall_memories(client, "b1", "q", 5, log) == ""
        assert log.text_of("warning")

    async def test_timeout_returns_empty_string(self):
        class SlowClient:
            async def recall(self, *args, **kwargs):
                import asyncio

                await asyncio.sleep(5)
                return {"results": []}

        log = Log()
        text = await recall_memories(SlowClient(), "b1", "q", 5, log, timeout=0.01)

        assert text == ""
        assert log.text_of("debug")

    async def test_none_results_is_tolerated(self):
        client = FakeClient(responses=[{"results": None}])

        assert await recall_memories(client, "b1", "q", 5, Log()) == ""
