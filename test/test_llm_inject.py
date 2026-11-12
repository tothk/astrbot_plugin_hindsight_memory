"""``HindsightMemory.on_llm_request`` 的召回注入行为。

这个钩子没法靠「看代码」保住：它一旦悄悄失效（比如又把记忆写回
``system_prompt``、或忘了 ``mark_as_temp()``），插件照常回话，只是不再
记得任何人。所以这里直接调未绑定的协程函数，用一个假 self 提供
``_memory``，不实例化整个插件（那要真建库、真读配置）。
"""

import pytest

from astrbot.core.agent.message import TextPart
from astrbot.core.platform import MessageType
from astrbot.core.provider.entities import ProviderRequest

from astrbot_plugin_hindsight_memory.core.recall import _HEADER, prune_previous_memories
from astrbot_plugin_hindsight_memory.main import HindsightMemory

MEMORY = "【相关记忆】以下是你们过往对话中与当前话题相关的片段：\n- 她上周加班到很晚"


class FakeMemory:
    """只实现 ``recall_for``，记录被问到什么。"""

    def __init__(self, memory: str = MEMORY) -> None:
        self.memory = memory
        self.queries: list[str] = []

    async def recall_for(self, text: str) -> str:
        self.queries.append(text)
        return self.memory


class FakeEvent:
    """钩子只用到消息类型与消息正文。"""

    def __init__(
        self,
        message_type=MessageType.FRIEND_MESSAGE,
        message_str: str = "今天好累啊",
    ) -> None:
        self._message_type = message_type
        self.message_str = message_str

    def get_message_type(self):
        return self._message_type


class FakeSelf:
    """替代 HindsightMemory 实例，只提供钩子会碰到的两个属性。"""

    def __init__(self, memory: FakeMemory) -> None:
        self._memory = memory


def build_request(prompt: str = "今天好累啊", system_prompt: str | None = None):
    """造一个最小的 ProviderRequest。"""
    req = ProviderRequest(prompt=prompt)
    req.system_prompt = system_prompt
    return req


async def call_hook(memory: FakeMemory, event: FakeEvent, req: ProviderRequest):
    """以未绑定方式调用钩子，避开 Star 的构造依赖。"""
    await HindsightMemory.on_llm_request(FakeSelf(memory), event, req)


class TestInjectsIntoUserMessage:
    async def test_memory_goes_into_extra_parts(self) -> None:
        memory = FakeMemory()
        req = build_request()

        await call_hook(memory, FakeEvent(), req)

        assert len(req.extra_user_content_parts) == 1
        assert req.extra_user_content_parts[0].text == MEMORY

    async def test_memory_is_marked_temporary(self) -> None:
        # 不加这个标记，召回的片段会被写进会话历史，每轮累积一份。
        req = build_request()

        await call_hook(FakeMemory(), FakeEvent(), req)

        assert req.extra_user_content_parts[0]._no_save is True

    async def test_system_prompt_is_left_untouched(self) -> None:
        # system_prompt 是上下文第 0 条，改它会让整条前缀缓存失效。
        req = build_request(system_prompt="你是小雪，一个温柔的女孩子。")

        await call_hook(FakeMemory(), FakeEvent(), req)

        assert req.system_prompt == "你是小雪，一个温柔的女孩子。"

    async def test_none_system_prompt_stays_none(self) -> None:
        # 以前 `f"{None}\n\n..."` 会把字面量 "None" 注进去，这里一并守住。
        req = build_request(system_prompt=None)

        await call_hook(FakeMemory(), FakeEvent(), req)

        assert req.system_prompt is None


class TestNoDuplicateInjection:
    async def test_calling_hook_twice_leaves_one_memory_part(self) -> None:
        # 热重载把 handler 重复注册时会跑两遍，裸 append 会叠出两段记忆。
        # 正文用真实格式（带页眉），否则认不出来源、也就谈不上剔除。
        req = build_request()

        await call_hook(FakeMemory(f"{_HEADER}\n- 旧记忆"), FakeEvent(), req)
        await call_hook(FakeMemory(f"{_HEADER}\n- 新记忆"), FakeEvent(), req)

        assert len(req.extra_user_content_parts) == 1
        assert req.extra_user_content_parts[0].text == f"{_HEADER}\n- 新记忆"

    async def test_previous_memory_is_replaced_not_appended(self) -> None:
        req = build_request()

        await call_hook(FakeMemory(f"{_HEADER}\n- 旧的"), FakeEvent(), req)
        await call_hook(FakeMemory(f"{_HEADER}\n- 新的"), FakeEvent(), req)

        texts = [part.text for part in req.extra_user_content_parts]
        assert texts == [f"{_HEADER}\n- 新的"]


class TestPrunePreviousMemories:
    async def test_other_plugins_parts_are_kept(self) -> None:
        # 知识库注入用的是同样的 _no_save 标记，只能靠页眉区分。
        req = build_request()
        kb_part = TextPart(text="[Related Knowledge Base Results]:\n文档片段")
        kb_part._no_save = True
        req.extra_user_content_parts.append(kb_part)

        await call_hook(FakeMemory(), FakeEvent(), req)

        texts = [part.text for part in req.extra_user_content_parts]
        assert "[Related Knowledge Base Results]:\n文档片段" in texts
        assert len(texts) == 2

    async def test_prune_mutates_the_list_in_place(self) -> None:
        # tool 循环的重查询持有这个列表对象本身，重新绑定它就白改了。
        req = build_request()
        original = req.extra_user_content_parts
        req.extra_user_content_parts.append(TextPart(text=f"{_HEADER}\n旧记忆"))

        removed = prune_previous_memories(req.extra_user_content_parts)

        assert removed == 1
        assert req.extra_user_content_parts is original
        assert req.extra_user_content_parts == []

    async def test_parts_without_text_attribute_are_ignored(self) -> None:
        # image_url / audio_url / think 这几类 part 没有 text 字段。
        req = build_request()
        req.extra_user_content_parts.append(object())

        removed = prune_previous_memories(req.extra_user_content_parts)

        assert removed == 0
        assert len(req.extra_user_content_parts) == 1

    async def test_returns_zero_when_nothing_to_prune(self) -> None:
        req = build_request()

        assert prune_previous_memories(req.extra_user_content_parts) == 0


class TestSkips:
    async def test_group_message_is_ignored(self) -> None:
        memory = FakeMemory()
        req = build_request()

        await call_hook(
            memory, FakeEvent(message_type=MessageType.GROUP_MESSAGE), req
        )

        assert req.extra_user_content_parts == []
        assert memory.queries == []

    async def test_empty_recall_adds_nothing(self) -> None:
        req = build_request()

        await call_hook(FakeMemory(memory=""), FakeEvent(), req)

        assert req.extra_user_content_parts == []

    async def test_empty_prompt_skips_recall(self) -> None:
        memory = FakeMemory()
        req = build_request(prompt="")

        await call_hook(memory, FakeEvent(message_str=""), req)

        assert memory.queries == []
        assert req.extra_user_content_parts == []


class TestQuerySource:
    async def test_uses_prompt_when_present(self) -> None:
        memory = FakeMemory()

        await call_hook(memory, FakeEvent(message_str="事件里的正文"), build_request("请求里的正文"))

        assert memory.queries == ["请求里的正文"]

    async def test_falls_back_to_event_message(self) -> None:
        memory = FakeMemory()
        req = build_request(prompt="")

        await call_hook(memory, FakeEvent(message_str="事件里的正文"), req)

        assert memory.queries == ["事件里的正文"]


class TestEndToEnd:
    async def test_memory_reaches_provider_but_not_history(self) -> None:
        """真正组装一次上下文，确认「模型看得到、历史里没有」。"""
        from astrbot.core.agent.message import Message, dump_messages_with_checkpoints

        req = build_request(prompt="今天好累啊")
        await call_hook(FakeMemory(), FakeEvent(), req)

        context = await req.assemble_context()
        blocks = context["content"]
        assert any("她上周加班到很晚" in block.get("text", "") for block in blocks)

        dumped = dump_messages_with_checkpoints([Message.model_validate(context)])
        assert "她上周加班到很晚" not in str(dumped)
