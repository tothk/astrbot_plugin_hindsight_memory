"""core/recorder.py 里纯函数的测试。

这几个函数不碰数据库、不碰 AstrBot，输入输出都能手工验证，
是这套测试里最便宜也最值钱的部分：抓取规则的每一次改动都会先在这里暴露。
"""

from datetime import datetime, timedelta, timezone

from astrbot_plugin_hindsight_memory.core.recorder import (
    build_raw_message,
    content_hash,
    resolve_bot_id,
    should_record_reply,
    should_record_user_text,
    strip_bot_at,
    strip_wake_prefix,
)


class TestStripWakePrefix:
    def test_default_prefix_is_removed(self):
        # AstrBot 只从 event.message_str 上剥前缀，outline 仍带着它。
        assert strip_wake_prefix("/今天好累", ["/"]) == "今天好累"

    def test_text_without_prefix_is_unchanged(self):
        assert strip_wake_prefix("今天好累", ["/"]) == "今天好累"

    def test_custom_prefix_is_removed(self):
        # 前缀被改成 "!" 时，不剥会把 "!" 一起写进记忆。
        assert strip_wake_prefix("!今天好累", ["!"]) == "今天好累"

    def test_only_the_first_prefix_is_removed(self):
        # 与 AstrBot 行为一致：命中即 break，不做循环剥离。
        assert strip_wake_prefix("/!你好", ["/", "!"]) == "!你好"

    def test_whitespace_after_prefix_is_stripped(self):
        assert strip_wake_prefix("/  今天好累 ", ["/"]) == "今天好累"

    def test_empty_prefix_is_ignored(self):
        # 空前缀会让 startswith() 恒为真，必须跳过，否则正文会被整个吃掉。
        assert strip_wake_prefix("今天好累", ["", "/"]) == "今天好累"

    def test_empty_prefix_list_keeps_text(self):
        # wake_prefix 配成 [] 表示不靠前缀唤醒，此时不该剥任何东西。
        assert strip_wake_prefix("/今天好累", []) == "/今天好累"

    def test_single_string_prefix_is_accepted(self):
        # 配置被人手写成字符串时，不能按字符逐个当前缀。
        assert strip_wake_prefix("/今天好累", "/") == "今天好累"

    def test_bare_prefix_becomes_empty(self):
        # 只发一个前缀，剥完是空串，交给 should_record_user_text 判空跳过。
        assert strip_wake_prefix("/", ["/"]) == ""

    def test_command_still_caught_after_stripping(self):
        # /reset 剥掉前缀后是 "reset" —— 这正是要点：
        # 剥前缀是为了让「聊天内容恰好以 / 开头」不被误杀，
        # 而真正的指令由 AstrBot 的 CommandFilter 处理，不会走到这里。
        text = strip_wake_prefix("/今天好累", ["/"])
        assert should_record_user_text(text) is True


class TestStripBotAt:
    """QQ 官方机器人私聊消息前面的 @机器人 占位符。"""

    def test_qq_official_prefix_is_removed(self):
        # 实测库里存的是 "[At:qq_official] 你看一下记忆存储插件……"。
        assert strip_bot_at("[At:qq_official] 今天好累", "qq_official") == "今天好累"

    def test_qq_official_prefix_without_text_becomes_empty(self):
        # 只 @ 不说话，剥完是空串，交给 should_record_user_text 判空跳过。
        assert strip_bot_at("[At:qq_official]", "qq_official") == ""

    def test_other_platform_is_untouched(self):
        # 别的适配器不会产生这个组件；用户自己打的这串字符不能被无声吃掉。
        assert strip_bot_at("[At:qq_official] 今天", "aiocqhttp") == "[At:qq_official] 今天"

    def test_qq_official_webhook_is_untouched(self):
        # webhook 版适配器不追加该组件，也不该剥。
        assert (
            strip_bot_at("[At:qq_official] 今天", "qq_official_webhook")
            == "[At:qq_official] 今天"
        )

    def test_text_without_prefix_on_qq_official(self):
        assert strip_bot_at("今天好累", "qq_official") == "今天好累"

    def test_prefix_in_the_middle_is_kept(self):
        # 只剥开头：正文中间出现的同样字符串是用户说的内容。
        assert strip_bot_at("今天 [At:qq_official] 好累", "qq_official") == (
            "今天 [At:qq_official] 好累"
        )

    def test_at_others_is_kept(self):
        # 只针对机器人自己，@别人是有意义的内容。
        assert strip_bot_at("[At:12345] 你好", "qq_official") == "[At:12345] 你好"

    def test_wake_prefix_still_caught_after_stripping_at(self):
        # 关键顺序：先剥 @ 再剥唤醒前缀，否则 "/今天好累" 匹配不到 "/"。
        text = strip_bot_at("[At:qq_official] /今天好累", "qq_official")
        assert strip_wake_prefix(text, ["/"]) == "今天好累"
        assert should_record_user_text(strip_wake_prefix(text, ["/"])) is True


class TestShouldRecordUserText:
    def test_normal_text_is_recorded(self):
        assert should_record_user_text("今天好累啊") is True

    def test_empty_text_is_skipped(self):
        # 纯图片/表情消息经 get_message_outline() 后可能是空串。
        assert should_record_user_text("") is False

    def test_slash_command_is_skipped(self):
        assert should_record_user_text("/reset") is False

    def test_fullwidth_slash_command_is_skipped(self):
        # 中文输入法下打出的全角斜杠，同样是指令。
        assert should_record_user_text("／help") is False

    def test_slash_in_the_middle_is_recorded(self):
        # 只有开头的斜杠才算指令，正文里的斜杠不该误伤。
        assert should_record_user_text("我们在 /tmp 下建个文件") is True


class TestShouldRecordReply:
    def test_normal_reply_is_recorded(self):
        assert (
            should_record_reply(
                role="assistant",
                text="我在呢",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is True
        )

    def test_error_reply_is_skipped(self):
        # agent 出异常时 AstrBot 会构造 role="err" 的响应。
        assert (
            should_record_reply(
                role="err",
                text="模型调用失败",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is False
        )

    def test_empty_reply_is_skipped(self):
        assert (
            should_record_reply(
                role="assistant",
                text="",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is False
        )

    def test_group_message_is_skipped(self):
        # 本插件只服务一对一陪伴场景，群聊不记录。
        assert (
            should_record_reply(
                role="assistant",
                text="大家好",
                is_private=False,
                stopped=False,
                stop_requested=False,
            )
            is False
        )

    def test_stopped_event_is_skipped(self):
        assert (
            should_record_reply(
                role="assistant",
                text="Output stopped.",
                is_private=True,
                stopped=True,
                stop_requested=False,
            )
            is False
        )

    def test_stop_requested_is_skipped(self):
        assert (
            should_record_reply(
                role="assistant",
                text="Output stopped.",
                is_private=True,
                stopped=False,
                stop_requested=True,
            )
            is False
        )

    def test_interruption_placeholder_is_skipped_even_without_stop_signal(self):
        """打断占位文本必须靠正文本身挡住。

        用户点停止时 AstrBot 会造一条 role="assistant"、正文为
        "Output stopped." 的伪回复，并且照样触发 on_llm_response 钩子。
        钩子运行时 astrbot/core/astr_agent_run_util.py 的 aborted 分支
        已经把 stopped 清掉、把 agent_stop_requested 复位成 False，
        两个符号信号都可能是假 —— 这时只能靠正文判定。
        """
        assert (
            should_record_reply(
                role="assistant",
                text="Output stopped.",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is False
        )

    def test_interruption_placeholder_with_surrounding_space_is_skipped(self):
        # 用 strip() 比较，避免占位符前后多出空格/换行时漏判。
        assert (
            should_record_reply(
                role="assistant",
                text="  Output stopped.\n",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is False
        )

    def test_reply_containing_the_placeholder_is_still_recorded(self):
        # 只有正文「正好」是占位符才跳过；模型正常谈论停止时不该误伤。
        assert (
            should_record_reply(
                role="assistant",
                text="你说 Output stopped. 是什么意思？",
                is_private=True,
                stopped=False,
                stop_requested=False,
            )
            is True
        )


class TestResolveBotId:
    def test_normal_self_id_is_used(self):
        assert resolve_bot_id("123456", "platform-abc") == "123456"

    def test_empty_self_id_falls_back_to_platform_id(self):
        assert resolve_bot_id("", "platform-abc") == "platform-abc"

    def test_unknown_self_id_falls_back_to_platform_id(self):
        # 部分适配器拿不到机器人账号，会返回这个固定占位串。
        assert resolve_bot_id("unknown_selfid", "platform-abc") == "platform-abc"


class TestContentHash:
    def test_same_content_gives_same_hash(self):
        first = content_hash("2026-11-08:umo", "user", "嗯嗯")
        second = content_hash("2026-11-08:umo", "user", "嗯嗯")
        assert first == second

    def test_hash_is_sha256_hex(self):
        digest = content_hash("2026-11-08:umo", "user", "嗯嗯")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_different_role_differs(self):
        # 同一句话由 user 说和由 llm 说，是两条记录。
        assert content_hash("d", "user", "好") != content_hash("d", "llm", "好")

    def test_different_document_differs(self):
        # 换一天或换一个会话，就不再判重。
        assert content_hash("d1", "user", "好") != content_hash("d2", "user", "好")

    def test_field_boundaries_are_unambiguous(self):
        # 没有分隔符时 "ab"+"c" 与 "a"+"bc" 会算出同一个指纹。
        assert content_hash("ab", "c", "x") != content_hash("a", "bc", "x")


class TestBuildRawMessage:
    # 用一个"本地时区"的固定时刻：build_raw_message 会把传入时间换算到本机时区，
    # 若这里传 UTC 的 21:30，在本机（+08:00）就变成次日 05:30，日期断言会飘。
    LOCAL = datetime.now().astimezone().tzinfo
    NOW = datetime(2026, 11, 8, 21, 30, 5, tzinfo=LOCAL)

    def _build(self, **overrides):
        kwargs = {
            "role": "user",
            "sender_id": "user-1",
            "text": "今天好累啊",
            "umo": "telegram:FriendMessage:42",
            "persona_id": "persona-1",
            "now": self.NOW,
        }
        kwargs.update(overrides)
        return build_raw_message(**kwargs)

    def test_fields_are_filled(self):
        raw = self._build()
        assert raw.role == "user"
        assert raw.sender_id == "user-1"
        assert raw.persona_id == "persona-1"
        assert raw.raw_message == "今天好累啊"
        assert raw.flushed == 0

    def test_record_date_comes_from_now(self):
        assert self._build().record_date == "2026-11-08"

    def test_document_id_is_day_plus_umo(self):
        # 文档边界是一天：同一天同一会话落进同一个 document_id。
        assert self._build().document_id == "2026-11-08:telegram:FriendMessage:42"

    def test_message_sent_time_is_local_isoformat(self):
        raw = self._build()
        # isoformat 带时区偏移与秒级精度，形如 2026-11-08T21:30:05+08:00。
        assert raw.message_sent_time.startswith("2026-11-08T21:30:05")
        assert raw.message_sent_time[19] in "+-"

    def test_utc_input_is_converted_to_local(self):
        # 传 UTC 时间也要落在本机时区的正确日期上。
        utc_now = datetime(2026, 11, 8, 21, 30, 5, tzinfo=timezone.utc)
        expected = utc_now.astimezone()
        assert self._build(now=utc_now).record_date == expected.date().isoformat()

    def test_record_date_matches_sent_time(self):
        # record_date 与 message_sent_time 必须描述同一天，否则跨零点的消息
        # 会出现「日期是昨天、时间戳是今天」，下游按天归档就会错。
        for now in (
            self.NOW,
            datetime(2026, 11, 8, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 11, 9, 0, 0, 1, tzinfo=timezone.utc),
        ):
            raw = self._build(now=now)
            assert raw.record_date == raw.message_sent_time[:10]

    def test_hash_matches_manual_computation(self):
        raw = self._build()
        assert raw.content_hash == content_hash(
            raw.document_id, raw.role, raw.raw_message
        )

    def test_same_content_same_datetime_gives_same_hash(self):
        # 这是去重能生效的前提：重复投递的同一事件算出同一指纹。
        assert self._build().content_hash == self._build().content_hash

    def test_different_day_gives_different_hash(self):
        other = self._build(now=self.NOW + timedelta(days=1))
        assert self._build().content_hash != other.content_hash
