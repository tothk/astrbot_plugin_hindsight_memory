"""配置解析、记忆格式化与定时计算的单元测试。

这些模块都是纯逻辑（不碰网络、不碰数据库），是最划算的测试对象：
一个断言就能锁住「配置写错时不许炸」，而这正是真实事故的来源。
"""

from datetime import datetime, time

import pytest

from astrbot_plugin_hindsight_memory.core.memory_service import (
    format_sync_result,
    parse_daily_time,
    seconds_until,
)
from astrbot_plugin_hindsight_memory.core.recall import format_memories
from astrbot_plugin_hindsight_memory.db.models.models import RawMessage
from astrbot_plugin_hindsight_memory.hindsight.plugin_config import (
    DEFAULT_BANK_ID,
    HindsightConfig,
    clean_nickname,
)
from astrbot_plugin_hindsight_memory.hindsight.sync import SyncOutcome, speaker_of


def _user_raw(*, sender_id: str = "10001", sender_name: str = "") -> RawMessage:
    return RawMessage(
        record_date="2026-11-10",
        document_id="d",
        sender_id=sender_id,
        sender_name=sender_name,
        persona_id="",
        role="user",
        raw_message="今天好累",
        message_sent_time="2026-11-10T23:50:00+08:00",
    )


def _llm_raw(*, sender_id: str = "bot", sender_name: str = "") -> RawMessage:
    return RawMessage(
        record_date="2026-11-10",
        document_id="d",
        sender_id=sender_id,
        sender_name=sender_name,
        persona_id="default",
        role="llm",
        raw_message="那就早点休息",
        message_sent_time="2026-11-10T23:51:00+08:00",
    )


class TestHindsightConfigFromMapping:
    def test_empty_mapping_falls_back_to_defaults(self):
        config = HindsightConfig.from_mapping({})

        assert config.host == "localhost"
        assert config.port == 8888
        assert config.token == ""
        assert config.batch_chars == 5000
        assert config.bank_id == DEFAULT_BANK_ID
        assert config.recall_quantity == 5
        assert config.daily_time == "00:05"

    def test_none_mapping_is_accepted(self):
        config = HindsightConfig.from_mapping(None)

        assert config.host == "localhost"

    def test_all_keys_are_read(self):
        config = HindsightConfig.from_mapping(
            {
                "hindsight_host": "192.168.100.102",
                "hindsight_port": 9000,
                "hindsight_token": "  sk-abc  ",
                "hindsight_bank_id": "dev",
                "sync_batch_chars": 2000,
                "sync_daily_time": "03:30",
                "recall_memory_quantity": 8,
            }
        )

        assert config.host == "192.168.100.102"
        assert config.port == 9000
        # Token 前后空白必须去掉：粘贴时最容易带进来，带空格会 401。
        assert config.token == "sk-abc"
        assert config.bank_id == "dev"
        assert config.batch_chars == 2000
        assert config.daily_time == "03:30"
        assert config.recall_quantity == 8

    def test_blank_strings_fall_back_to_defaults(self):
        config = HindsightConfig.from_mapping(
            {"hindsight_host": "   ", "hindsight_bank_id": "", "sync_daily_time": " "}
        )

        assert config.host == "localhost"
        assert config.bank_id == DEFAULT_BANK_ID
        assert config.daily_time == "00:05"

    def test_nicknames_default_to_empty(self):
        config = HindsightConfig.from_mapping({})

        assert config.user_nickname == ""
        assert config.llm_nickname == ""

    def test_nicknames_are_read_and_cleaned(self):
        config = HindsightConfig.from_mapping(
            {"user_nickname": "  小\n满  ", "llm_nickname": "小满的AI"}
        )

        assert config.user_nickname == "小 满"
        assert config.llm_nickname == "小满的AI"

    def test_non_numeric_port_falls_back(self):
        config = HindsightConfig.from_mapping({"hindsight_port": "abc"})

        assert config.port == 8888

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 1), (-5, 1), (70000, 65535)],
    )
    def test_port_is_clamped(self, raw, expected):
        assert HindsightConfig.from_mapping({"hindsight_port": raw}).port == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 1), (-1, 1), (99999, 50000)],
    )
    def test_batch_chars_is_clamped(self, raw, expected):
        config = HindsightConfig.from_mapping({"sync_batch_chars": raw})

        assert config.batch_chars == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 1), (-3, 1), (999, 50)],
    )
    def test_recall_quantity_is_clamped(self, raw, expected):
        config = HindsightConfig.from_mapping({"recall_memory_quantity": raw})

        assert config.recall_quantity == expected

    def test_float_string_port_is_coerced(self):
        # 面板里手改 JSON 写出 "8888.0" 这类值时，int() 也能吃下 "8888"。
        config = HindsightConfig.from_mapping({"hindsight_port": "8888"})

        assert config.port == 8888


class TestFormatMemories:
    def test_empty_results_yield_empty_string(self):
        assert format_memories([], 5) == ""

    def test_zero_limit_yields_empty_string(self):
        results = [{"text": "她喜欢猫", "type": "observation"}]

        assert format_memories(results, 0) == ""

    def test_observation_is_placed_first(self):
        results = [
            {"text": "world fact", "type": "world"},
            {"text": "她最近工作压力大", "type": "observation"},
        ]

        text = format_memories(results, 5)
        lines = text.splitlines()

        assert "她最近工作压力大" in lines[1]
        assert "world fact" in lines[2]

    def test_type_order_is_stable_within_group(self):
        results = [
            {"text": "第一个 world", "type": "world"},
            {"text": "第二个 world", "type": "world"},
        ]

        lines = format_memories(results, 5).splitlines()

        assert "第一个 world" in lines[1]
        assert "第二个 world" in lines[2]

    def test_limit_truncates_after_observation_first(self):
        results = [
            {"text": "world 1", "type": "world"},
            {"text": "obs 1", "type": "observation"},
            {"text": "obs 2", "type": "observation"},
        ]

        lines = format_memories(results, 2).splitlines()

        # 第 0 行是标题，所以保留的 2 条占第 1、2 行。
        assert len(lines) == 3
        assert "obs 1" in lines[1]
        assert "obs 2" in lines[2]

    def test_items_without_text_are_skipped(self):
        results = [
            {"text": "", "type": "observation"},
            {"type": "observation"},
            {"text": "有效事实", "type": "world"},
        ]

        lines = format_memories(results, 5).splitlines()

        assert len(lines) == 2
        assert "有效事实" in lines[1]

    def test_text_is_stripped(self):
        results = [{"text": "  她喜欢猫  ", "type": "world"}]

        assert "- 她喜欢猫" in format_memories(results, 5)

    def test_header_is_present(self):
        results = [{"text": "她喜欢猫", "type": "world"}]

        text = format_memories(results, 5)

        assert text.splitlines()[0].startswith("以下是你对该用户既往对话的记忆片段")


class TestParseDailyTime:
    def test_valid_time(self):
        assert parse_daily_time("03:30") == time(hour=3, minute=30)

    def test_padded_time(self):
        assert parse_daily_time("00:05") == time(hour=0, minute=5)

    @pytest.mark.parametrize("raw", ["", "abc", "25:00", "12", "12:99", None])
    def test_invalid_values_fall_back_to_default(self, raw):
        # 配置写错不该让插件起不来，退回默认时刻即可。
        assert parse_daily_time(raw) == time(hour=0, minute=5)


class TestSecondsUntil:
    def test_target_later_today(self):
        now = datetime(2026, 11, 10, 23, 0, 0)

        assert seconds_until(time(23, 30), now) == pytest.approx(1800.0)

    def test_target_already_passed_rolls_to_tomorrow(self):
        now = datetime(2026, 11, 10, 23, 50, 0)

        # 相差 24 小时减 10 分钟。
        assert seconds_until(time(23, 40), now) == pytest.approx(23 * 3600 + 50 * 60)

    def test_target_equal_to_now_rolls_to_tomorrow(self):
        now = datetime(2026, 11, 10, 0, 5, 0)

        # 恰好等于当前时刻时不能返回 0，否则会立刻触发一次，然后天天错位。
        assert seconds_until(time(0, 5), now) == pytest.approx(86400.0)

    def test_result_is_always_positive(self):
        now = datetime(2026, 11, 10, 12, 0, 0)

        for hour in range(24):
            assert seconds_until(time(hour, 0), now) > 0


class TestSpeakerOf:
    def test_uses_sender_name_when_present(self):
        raw = RawMessage(
            record_date="2026-11-10",
            document_id="d",
            sender_id="10001",
            sender_name="小满",
            persona_id="",
            role="user",
            raw_message="今天好累",
            message_sent_time="2026-11-10T23:50:00+08:00",
        )

        assert speaker_of(raw) == "小满"

    def test_falls_back_to_sender_id(self):
        raw = RawMessage(
            record_date="2026-11-10",
            document_id="d",
            sender_id="10001",
            persona_id="",
            role="user",
            raw_message="今天好累",
            message_sent_time="2026-11-10T23:50:00+08:00",
        )

        assert speaker_of(raw) == "10001"

    def test_blank_sender_name_falls_back(self):
        raw = RawMessage(
            record_date="2026-11-10",
            document_id="d",
            sender_id="10001",
            sender_name="   ",
            persona_id="",
            role="user",
            raw_message="今天好累",
            message_sent_time="2026-11-10T23:50:00+08:00",
        )

        assert speaker_of(raw) == "10001"

    def test_configured_user_name_wins(self):
        raw = _user_raw(sender_name="A165F5ED796689E7CF11392EB290C0E9")

        assert speaker_of(raw, user_name="小满") == "小满"

    def test_configured_bot_name_wins_for_llm_rows(self):
        raw = _llm_raw(sender_name="助手")

        assert speaker_of(raw, bot_name="小满的AI") == "小满的AI"

    def test_user_name_does_not_apply_to_llm_rows(self):
        # 配错了称呼不该串到对方身上：AI 那一行必须还用 AI 的称呼。
        raw = _llm_raw(sender_name="助手")

        assert speaker_of(raw, user_name="小满") == "助手"

    def test_bot_name_does_not_apply_to_user_rows(self):
        raw = _user_raw(sender_name="小满")

        assert speaker_of(raw, bot_name="小满的AI") == "小满"

    def test_blank_configured_name_falls_back_to_record(self):
        raw = _user_raw(sender_name="小满")

        assert speaker_of(raw, user_name="   ") == "小满"

    def test_configured_name_survives_missing_sender_name(self):
        # 这正是配置要救的情况：平台只给了哈希 id，昵称拿不到。
        raw = _user_raw(
            sender_id="A165F5ED796689E7CF11392EB290C0E9", sender_name=""
        )

        assert speaker_of(raw, user_name="小满") == "小满"


class TestCleanNickname:
    def test_strips_newlines(self):
        # 换行必须去掉：会被 Hindsight 当作行分隔，其后那行没有说话人。
        assert clean_nickname("小\n满") == "小 满"

    def test_strips_crlf(self):
        assert clean_nickname("小\r\n满") == "小 满"

    def test_collapses_runs_of_whitespace(self):
        assert clean_nickname("  小 \t 满  ") == "小 满"

    def test_keeps_parentheses(self):
        # 括号只影响观感，不影响 Name (time): text 的解析，允许保留。
        assert clean_nickname("小满(AI)") == "小满(AI)"

    def test_none_becomes_empty(self):
        assert clean_nickname(None) == ""

    def test_non_string_is_coerced(self):
        assert clean_nickname(123) == "123"


class TestFormatSyncResult:
    def test_success_reports_counts(self):
        outcome = SyncOutcome(messages=31, batches=4, documents=6, flushed=31)

        text = format_sync_result(outcome)

        assert "31/31" in text
        assert "4 批" in text
        assert "6 个文档" in text

    def test_nothing_to_do_has_its_own_message(self):
        outcome = SyncOutcome(messages=0, batches=0, documents=0, flushed=0)

        assert "没有待提交的记录" in format_sync_result(outcome)

    def test_failure_surfaces_error(self):
        outcome = SyncOutcome(error="无法连接 Hindsight（http://h:8888）")

        text = format_sync_result(outcome)

        assert text.startswith("❌")
        assert "无法连接 Hindsight" in text

    def test_partial_flush_is_not_reported_as_success_line(self):
        # 失败但已经投出去一部分：仍按失败讲述，避免用户误以为全部写完了。
        outcome = SyncOutcome(
            messages=31, batches=4, documents=6, flushed=10, error="5xx"
        )

        text = format_sync_result(outcome)

        assert text.startswith("❌")
        assert "10/31" not in text
