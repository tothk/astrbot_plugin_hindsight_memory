"""core/capture.py 里守卫判定的测试。

is_command_event 是唯一无法做成"纯函数"的守卫：它要读宿主写在事件上的
activated_handlers。这里用假事件对象把这条判定单独钉住，不需要真的
启动 AstrBot。
"""

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot.core.star.filter.regex import RegexFilter

from astrbot_plugin_hindsight_memory.core.capture import is_command_event


class FakeHandler:
    """宿主在 activated_handlers 里放的 handler 元数据，这里只用到过滤器。"""

    def __init__(self, *event_filters) -> None:
        self.event_filters = list(event_filters)


class FakeEvent:
    """只实现 is_command_event 会用到的那一个方法。"""

    def __init__(self, handlers=None, *, raise_exc: bool = False) -> None:
        self._handlers = handlers
        self._raise = raise_exc

    def get_extra(self, key: str):
        assert key == "activated_handlers"
        if self._raise:
            raise RuntimeError("boom")
        return self._handlers


class TestIsCommandEvent:
    def test_command_filter_marks_command(self):
        event = FakeEvent([FakeHandler(CommandFilter("help"))])
        assert is_command_event(event) is True

    def test_command_group_filter_marks_command(self):
        event = FakeEvent([FakeHandler(CommandGroupFilter("admin"))])
        assert is_command_event(event) is True

    def test_regex_filter_marks_command(self):
        # 正则处理器在宿主里同样不进 LLM，所以也算指令。
        event = FakeEvent([FakeHandler(RegexFilter(".*"))])
        assert is_command_event(event) is True

    def test_plain_handler_is_not_command(self):
        # 只有 event_message_type 之类的过滤器时，这是一条普通聊天。
        event = FakeEvent([FakeHandler(EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE))])
        assert is_command_event(event) is False

    def test_no_activated_handlers_is_not_command(self):
        assert is_command_event(FakeEvent(None)) is False

    def test_empty_activated_handlers_is_not_command(self):
        assert is_command_event(FakeEvent([])) is False

    def test_second_handler_can_be_command(self):
        # 一条消息可以同时命中多个 handler，任一是指令即为指令。
        event = FakeEvent([FakeHandler(), FakeHandler(CommandFilter("reset"))])
        assert is_command_event(event) is True

    def test_should_record_user_text_respects_is_command(self):
        # 关键场景：剥掉前缀后正文是 "reset"，光看文本认不出来。
        from astrbot_plugin_hindsight_memory.core.recorder import (
            should_record_user_text,
        )

        assert should_record_user_text("reset") is True
        assert should_record_user_text("reset", is_command=True) is False
