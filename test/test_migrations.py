"""数据库版本迁移的测试。

这里的「老库」都用裸 SQL 现场造：迁移要处理的是历史结构，用当前模型建库只会
造出最新版、测不到任何东西。

守的边界：全新库怎么标版本、老库怎么被认成 v1 再爬到 v2、在途批次怎么被退回、
降级怎么被拒、认不出的库怎么报错。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from astrbot_plugin_hindsight_memory.config.plugin_paths import PluginPaths
from astrbot_plugin_hindsight_memory.db.migrations import (
    BASELINE_VERSION,
    CURRENT_VERSION,
    MIGRATIONS,
    run_migrations,
)
from astrbot_plugin_hindsight_memory.db.models.sql_model_manager import SqlModelManager

# v1 的形状：有 content_hash、没有 operation_id，批次归属靠 id 区间。
# v0.0.1 发出去的就是这个结构。
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
CREATE TABLE syncoperation (
    id INTEGER NOT NULL PRIMARY KEY,
    operation_id VARCHAR NOT NULL,
    first_row_id INTEGER NOT NULL,
    last_row_id INTEGER NOT NULL,
    items_count INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL,
    error VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
"""

# 迁移会加出来的列。比对「数据没被改写」时要把它排除掉，否则会误报。
_ADDED_BY_V2 = {"operation_id"}


def _make_engine(db_file):
    return create_engine(f"sqlite:///{db_file.as_posix()}")


def _make_legacy(db_file, rows=2, *, flushed=0, with_operation=False):
    """造一个 v1 的库，返回写入的行数。

    Args:
        db_file: 库文件路径。
        rows: 写入几条记录。
        flushed: 这些记录的投递状态。
        with_operation: 是否同时写一条批次凭据（模拟「升级时还有批在途」）。
    """
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
                    " VALUES (:d, :doc, :s, :p, :r, :m, :t, :h, :f)"
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
                    "f": flushed,
                },
            )
        if with_operation:
            conn.exec_driver_sql(
                "INSERT INTO syncoperation (operation_id, first_row_id, last_row_id,"
                " items_count, status, attempts, error, created_at, updated_at)"
                " VALUES ('op-inflight', 1, 2, 2, 'pending', 0, '',"
                " '2026-11-01T10:00:00+08:00', '2026-11-01T10:00:00+08:00')"
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


def _read_columns(db_file, table="rawmessage"):
    engine = _make_engine(db_file)
    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
        }
    engine.dispose()
    return columns


def _count_operations(db_file):
    engine = _make_engine(db_file)
    with engine.connect() as conn:
        count = conn.exec_driver_sql("SELECT COUNT(*) FROM syncoperation").scalar_one()
    engine.dispose()
    return count


def test_fresh_db_gets_current_version(workdir):
    """全新库没有表，直接标到当前版本，不必走迁移链。"""
    db_file = workdir / "new.db"
    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()
    assert _read_version(db_file) == CURRENT_VERSION


def test_legacy_db_is_upgraded_without_losing_data(workdir):
    """版本化之前的库：爬到 v2，加一列，其余数据逐字段不变。"""
    db_file = workdir / "legacy.db"
    _make_legacy(db_file, rows=2)
    before = _read_rows(db_file)

    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()

    assert _read_version(db_file) == CURRENT_VERSION
    assert "operation_id" in _read_columns(db_file)

    after = _read_rows(db_file)
    assert len(after) == len(before)
    for old, new in zip(before, after):
        # 只允许新增列，原有列一个都不许改
        assert {k: v for k, v in new.items() if k not in _ADDED_BY_V2} == old
    assert [row["raw_message"] for row in after] == ["第 0 句话", "第 1 句话"]


def test_legacy_inflight_batches_are_released(workdir):
    """升级时在途的批次退回待投递，凭据清空。

    这些行在 v1 里的归属只存在 id 区间中，加列之后区间不再参与回写。若让它们
    继续停在「已入队」，对账会删掉凭据、行却永远等不到结论——比原 bug 更糟。
    """
    db_file = workdir / "inflight.db"
    _make_legacy(db_file, rows=2, flushed=1, with_operation=True)
    assert _count_operations(db_file) == 1

    engine = _make_engine(db_file)
    run_migrations(engine)
    engine.dispose()

    rows = _read_rows(db_file)
    assert all(row["flushed"] == 0 for row in rows), rows
    assert all(row["operation_id"] == "" for row in rows), rows
    assert _count_operations(db_file) == 0


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


def test_migration_step_is_idempotent(workdir):
    """v1→v2 本身能重跑：跑两遍不该报 duplicate column。"""
    db_file = workdir / "rerun.db"
    _make_legacy(db_file, rows=1)

    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    # 把版本号退回 v1，再爬一次；列已经在了，函数必须靠「列存在就跳过」活下来
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {BASELINE_VERSION}")
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()


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


def test_legacy_is_stamped_as_baseline_not_latest(workdir):
    """老库必须先被认成 v1，再爬到最新版。

    直接盖成 CURRENT_VERSION 会让 v1→v2 被跳过：列没加、索引没建，
    启动时才炸在「no such column」。这条断言守的就是那个岔路。
    """
    assert BASELINE_VERSION < CURRENT_VERSION

    db_file = workdir / "baseline.db"
    _make_legacy(db_file, rows=1)

    engine = _make_engine(db_file)
    from astrbot_plugin_hindsight_memory.db.migrations import _detect_baseline

    with engine.begin() as conn:
        assert _detect_baseline(conn) == BASELINE_VERSION
        # 且这次只写版本号，还没动结构
        assert "operation_id" not in _read_columns(db_file)
    engine.dispose()

    engine = _make_engine(db_file)
    assert run_migrations(engine) == CURRENT_VERSION
    engine.dispose()
    assert "operation_id" in _read_columns(db_file)


def test_registry_covers_every_version():
    """MIGRATIONS 的 key 必须是从 2 开始连续递增到 CURRENT_VERSION。

    少一个 key 会让爬升在 ``MIGRATIONS[target]`` 处抛 KeyError；多一个用不上的
    key 则是永远不会执行的死代码。两种都只有断言能发现。
    """
    assert sorted(MIGRATIONS) == list(range(2, CURRENT_VERSION + 1))


def test_manager_upgrades_on_startup(workdir, log):
    """走一遍真实入口：SqlModelManager 打开老库时先迁到 v2 再建表。"""
    plugin_data = workdir / "plugin_data"
    paths = PluginPaths(plugin_data)
    db_file = paths.sqlitedb_dir / SqlModelManager._DB_FILENAME
    _make_legacy(db_file, rows=2)

    manager = SqlModelManager(paths, log)
    try:
        # 老库的两行都还是待投递
        assert len(manager.list_unflushed(limit=100)) == 2
        assert manager.count_by_flushed() == {0: 2}
    finally:
        manager.close()

    assert _read_version(db_file) == CURRENT_VERSION
    assert "operation_id" in _read_columns(db_file)
    # 启动日志里应该看得到「按 v1 处理」和迁移步骤，便于排查
    assert any("标记为 v" in msg for msg in log.text_of("warning")), log.messages
    assert any("v1 → v2" in msg for msg in log.text_of("info")), log.messages
