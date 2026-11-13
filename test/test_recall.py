"""core/recall.py 的单元测试。

重点在「库不存在时自动建库并重试一次」这条分支——它是首次安装唯一的
自救路径，出错时表现是「悄悄没有记忆」，最难被发现。
"""

import pytest

from astrbot_plugin_hindsight_memory.core.recall import (
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

    async def test_creates_bank_then_retries_once(self):
        client = FakeClient(
            responses=[
                HindsightBankNotFoundError("Bank 'b1' not found"),
                {"results": [{"text": "她喜欢猫"}]},
            ]
        )
        log = Log()

        data = await _recall_or_create_bank(client, "b1", "q", log)

        assert client.calls == [
            ("recall", "b1", "q"),
            ("ensure_bank", "b1"),
            ("recall", "b1", "q"),
        ]
        assert data["results"][0]["text"] == "她喜欢猫"
        assert any("不存在" in m for m in log.text_of("info"))

    async def test_second_404_propagates_instead_of_looping(self):
        # 建库后还 404 说明问题不在「库缺失」，继续重试就是死循环。
        client = FakeClient(
            responses=[
                HindsightBankNotFoundError("not found"),
                HindsightBankNotFoundError("still not found"),
            ]
        )

        with pytest.raises(HindsightBankNotFoundError):
            await _recall_or_create_bank(client, "b1", "q", Log())

        assert sum(1 for c in client.calls if c[0] == "ensure_bank") == 1

    async def test_uses_low_budget(self):
        # 服务端默认 mid 会稳定跑满 3 秒超时，这里钉死成 low。
        client = FakeClient()

        await _recall_or_create_bank(client, "b1", "q", Log())

        assert client.keywords[0]["budget"] == "low"

    async def test_retry_after_create_also_uses_low_budget(self):
        # 两次 recall 都要带 budget：只改第一次的话，首次安装（必然走建库
        # 分支）的那一轮仍是 mid。
        client = FakeClient(
            responses=[HindsightBankNotFoundError("not found"), {"results": []}]
        )

        await _recall_or_create_bank(client, "b1", "q", Log())

        assert [kw["budget"] for kw in client.keywords] == ["low", "low"]

    async def test_create_failure_propagates(self):
        client = FakeClient(
            responses=[HindsightBankNotFoundError("not found")], create_ok=False
        )

        with pytest.raises(HindsightConnectionError):
            await _recall_or_create_bank(client, "b1", "q", Log())


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

    async def test_bank_not_found_is_handled_internally(self):
        # 库不存在不该冒到调用方：会先建库再重试，成功即正常返回。
        client = FakeClient(
            responses=[
                HindsightBankNotFoundError("not found"),
                {"results": [{"text": "她喜欢猫", "type": "world"}]},
            ]
        )

        text = await recall_memories(client, "b1", "q", 5, Log())

        assert "她喜欢猫" in text

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
