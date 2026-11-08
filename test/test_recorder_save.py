"""Recorder.save 的行为测试：真实 sqlite + 假数据库两种路径。

这里用 tmp_path 下的真库跑，因为要验证的是"写进去之后拿得回来"以及
"唯一索引真的挡住了重复"——这两件事用假对象测不出来。
"""

from datetime import datetime

import pytest
from sqlmodel import Session, select

from conftest import BoomSql, ListLog
from astrbot_plugin_hindsight_memory.core.recorder import Recorder, build_raw_message
from astrbot_plugin_hindsight_memory.db.models.models import RawMessage

NOW = datetime(2026, 11, 8, 21, 30, 5)


def make_raw(**overrides):
    """造一条待写入的记录，默认内容固定，便于断言。"""
    kwargs = {
        "role": "user",
        "sender_id": "user-1",
        "text": "今天好累啊",
        "umo": "telegram:FriendMessage:42",
        "persona_id": "persona-1",
        "now": NOW,
    }
    kwargs.update(overrides)
    return build_raw_message(**kwargs)


class Notifier:
    """记录被调用次数的假通知回调。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


class TestSaveSuccess:
    async def test_row_is_written(self, sql, log: ListLog):
        recorder = Recorder(sql, log)
        notify = Notifier()

        saved = await recorder.save(make_raw(), notify)

        assert saved is True
        assert notify.calls == 0
        with Session(sql._engine) as session:
            rows = session.exec(select(RawMessage)).all()
        assert len(rows) == 1
        assert rows[0].raw_message == "今天好累啊"
        assert rows[0].role == "user"

    async def test_success_is_logged_with_id(self, sql, log: ListLog):
        recorder = Recorder(sql, log)

        await recorder.save(make_raw(), Notifier())

        infos = log.text_of("info")
        assert any("已记录" in msg and "id=1" in msg for msg in infos)

    async def test_duplicate_is_skipped_not_treated_as_failure(self, sql, log: ListLog):
        # 同一份内容写两次：第二次被唯一索引挡下，但不算失败。
        recorder = Recorder(sql, log)
        notify = Notifier()

        first = await recorder.save(make_raw(), notify)
        second = await recorder.save(make_raw(), notify)

        assert first is True
        assert second is True
        assert notify.calls == 0
        with Session(sql._engine) as session:
            assert len(session.exec(select(RawMessage)).all()) == 1
        assert any("内容重复" in msg for msg in log.text_of("info"))

    async def test_duplicate_does_not_leak_error_log(self, sql, log: ListLog):
        recorder = Recorder(sql, log)

        await recorder.save(make_raw(), Notifier())
        await recorder.save(make_raw(), Notifier())

        assert log.text_of("error") == []

    async def test_different_role_is_not_a_duplicate(self, sql, log: ListLog):
        recorder = Recorder(sql, log)

        await recorder.save(make_raw(role="user"), Notifier())
        await recorder.save(make_raw(role="llm", sender_id="bot-1"), Notifier())

        with Session(sql._engine) as session:
            assert len(session.exec(select(RawMessage)).all()) == 2


class TestSaveFailure:
    async def test_failure_returns_false_and_notifies(self, log: ListLog):
        recorder = Recorder(BoomSql(), log)
        notify = Notifier()

        saved = await recorder.save(make_raw(), notify)

        assert saved is False
        assert notify.calls == 1
        assert any("database is locked" in msg for msg in log.text_of("error"))

    async def test_repeated_failure_notifies_only_once(self, log: ListLog):
        # database is locked 是持续性故障，每句对话要写两行，
        # 逐条提示会刷屏，所以只在首次失败时提示。
        recorder = Recorder(BoomSql(), log)
        notify = Notifier()

        await recorder.save(make_raw(), notify)
        await recorder.save(make_raw(text="第二句"), notify)
        await recorder.save(make_raw(text="第三句"), notify)

        assert notify.calls == 1

    async def test_success_after_failure_resets_throttle(self, log: ListLog):
        # 故障恢复过一次之后，再次故障应当能重新提示。
        sql = BoomSql()
        recorder = Recorder(sql, log)
        notify = Notifier()

        await recorder.save(make_raw(), notify)
        assert notify.calls == 1

        sql.insert_raw = lambda raw: raw  # 模拟恢复正常
        await recorder.save(make_raw(text="恢复了"), notify)
        assert notify.calls == 1

        sql.insert_raw = BoomSql().insert_raw  # 再次故障
        await recorder.save(make_raw(text="又坏了"), notify)
        assert notify.calls == 2

    async def test_error_message_is_logged_every_time(self, log: ListLog):
        # 提示节流了，但日志不该节流，否则排查时看不到后续失败。
        recorder = Recorder(BoomSql(), log)
        notify = Notifier()

        await recorder.save(make_raw(), notify)
        await recorder.save(make_raw(text="第二句"), notify)

        assert len(log.text_of("error")) == 2
