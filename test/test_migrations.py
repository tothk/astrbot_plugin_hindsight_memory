"""数据库版本迁移的测试。

这里的「老库」都用裸 SQL 现场造：迁移要处理的是版本化之前的结构，用当前模型
建库只会造出最新版、测不到任何东西。

当前 ``MIGRATIONS`` 是空的（v1 就是初始结构），所以这组测试守的是边界行为：
全新库怎么标版本、老库怎么被认成 v1、降级怎么被拒、认不出的库怎么报错。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from astrbot_plugin_hindsight_memory.config.plugin_paths import PluginPaths
from astrbot_plugin_hindsight_memory.db.migrations import (
    CURRENT_VERSION,
    MIGRATIONS,
    run_migrations,
)
from astrbot_plugin_hindsight_memory.db.models.sql_model_manager import SqlModelManager

# 版本化之前的库：有 content_hash，没有版本号。v0.0.1 发出去的就是这个形状。
LEGACY_DDL = """
CREATE TABLE rawmessage (
    id INTEGER NOT NULL PRIMARY KEY,
    record_date VARCHAR NOT NULL,
    document_id VARCHAR NOT NULL,
    sender_id VARCHAR NOT NULL,
    persona_id VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    raw_message VARCHAR NOT NULL,
    message_sent_time VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    flushed INTEGER NOT NULL
);
CREATE UNIQUE INDEX ix_rawmessage_content_hash
    ON rawmessage (content_hash);
"""


def _make_engine(db_file):
    return create_engine(f"sqlite:///{db_file.as_posix()}")


def _make_legacy(db_file, rows=2):
    """造一个版本化之前的库，返回写入的行数。"""
    engine = _make_engine(db_file)
    with engine.begin() as conn:
        for statement in LEGACY_DDL.strip().split(";"):
            if statement.strip():
                conn.exec_driver_sql(statement)
        for index in range(rows):
            conn.execute(
                text(
                    "INSERT INTO rawmessage (record_date, document_id, sender_id,"
                    " persona_id, role, raw_message, message_sent_time,"
                    " content_hash, flushed)"
                    " VALUES (:d, :doc, :s, :p, :r, :m, :t, :h, 0)"
                ),
                {
                    "d": "2026-11-01",
                    "doc": f"2026-11-01:umo:{index}",
                    "s": f"sender-{index}",
                    "p": "default",
                    "r": "user",
                    "m": f"第 {index} 句话",
                    "t": "2026-11-01T10:00:00+08:00",
                    "h": f"hash-{index}",
                },
            )
    engine.dispose()
    return rows


def _read_version(db_file):
    engine = _make_engine(db_file)
    with engine.connect() as conn:
        version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
    engine.dispose()
    return version


def _read_rows(db_file):
    engine = _make_engine(db_file)
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.exec_driver_sql("SELECT * FROM rawmessage").mappings()
        ]
    engine.dispose()
    return rows


def test_fresh_db_gets_current_version(workdir):
    """全新库没有表，直接标到当前版本。"""
    db_file = workdir / "new.db"
    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()
    assert _read_version(db_file) == CURRENT_VERSION


def test_legacy_db_is_stamped_without_touching_data(workdir):
    """版本化之前的库：只打版本号，表和数据一律不动。"""
    db_file = workdir / "legacy.db"
    _make_legacy(db_file, rows=2)
    before = _read_rows(db_file)

    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()

    assert _read_version(db_file) == CURRENT_VERSION
    # 逐字段比对：迁移不该改写任何一列
    assert _read_rows(db_file) == before
    assert [row["raw_message"] for row in before] == ["第 0 句话", "第 1 句话"]


def test_stamping_is_idempotent(workdir):
    """连跑三次，版本号不变、数据不变。"""
    db_file = workdir / "twice.db"
    _make_legacy(db_file, rows=1)

    engine = _make_engine(db_file)
    for _ in range(3):
        assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()

    assert len(_read_rows(db_file)) == 1
    assert _read_version(db_file) == CURRENT_VERSION


def test_downgrade_is_refused(workdir):
    """库版本比插件新时直接报错，而不是拿旧代码去读新结构。"""
    db_file = workdir / "future.db"
    engine = _make_engine(db_file)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {CURRENT_VERSION + 1}")

    with pytest.raises(RuntimeError, match="高于插件支持"):
        run_migrations(engine)
    engine.dispose()


def test_unrecognized_baseline_raises(workdir):
    """有 rawmessage 但没有 content_hash，说明比 v1 还老，不能猜着补。"""
    db_file = workdir / "ancient.db"
    engine = _make_engine(db_file)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE rawmessage (id INTEGER PRIMARY KEY, raw_message VARCHAR)"
        )

    with pytest.raises(RuntimeError, match="无法识别"):
        run_migrations(engine)
    engine.dispose()


def test_registry_is_empty_at_v1():
    """v1 是初始结构，不该有迁移步骤。

    这条断言是给未来的人看的：如果有人在没改 CURRENT_VERSION 的情况下往
    MIGRATIONS 里塞了一个函数，这里会失败，提醒他两步要一起改。
    """
    assert CURRENT_VERSION == 1
    assert MIGRATIONS == {}


def test_registry_keys_are_contiguous():
    """MIGRATIONS 的 key 必须是从 2 开始连续递增的目标版本。

    少一个 key 会让爬升在 ``MIGRATIONS[target]`` 处抛 KeyError；多一个用不上的
    key 则是永远不会执行的死代码。两种都只有断言能发现。
    """
    assert sorted(MIGRATIONS) == list(range(2, CURRENT_VERSION + 1))


def test_manager_stamps_on_startup(workdir, log):
    """走一遍真实入口：SqlModelManager 打开老库时应该先打版本号再建表。"""
    plugin_data = workdir / "plugin_data"
    paths = PluginPaths(plugin_data)
    db_file = paths.sqlitedb_dir / SqlModelManager._DB_FILENAME
    _make_legacy(db_file, rows=2)

    manager = SqlModelManager(paths, log)
    try:
        # 老库的两行都还是待投递
        assert len(manager.list_unflushed(limit=100)) == 2
    finally:
        manager.close()

    assert _read_version(db_file) == CURRENT_VERSION
    # 启动日志里应该看得到「按 v1 处理」的提示，便于排查
    assert any("标记为 v" in msg for msg in log.text_of("warning")), log.messages
