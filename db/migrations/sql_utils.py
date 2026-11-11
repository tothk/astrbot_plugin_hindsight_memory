"""迁移里要用的裸 SQL 小工具。

迁移必须绕开 ORM 模型去读「上一版」的表：模型描述的是当前形状，用它读旧表
会因多出来的列报错、因少掉的列静默取到 None。所以这里读结构走 PRAGMA、
读数据走 ``SELECT *``，只有写回时才用 ``Table.insert()``。
"""

from typing import Any

from sqlalchemy.engine import Connection


def get_user_version(conn: Connection) -> int:
    """读数据库的结构版本。

    ``PRAGMA user_version`` 是跟着文件头走的整数，不占表、删表重建也不影响它，
    比「自己建一张 schema_version 表」可靠：读版本号这件事本身不该依赖表结构。
    """
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def set_user_version(conn: Connection, version: int) -> None:
    """写数据库的结构版本。

    PRAGMA 不接受参数绑定，只能拼字符串。传入的版本号来自代码里的常量或
    ``get_user_version()`` 的读回值，不来自外部输入；仍用 ``int()`` 兜一道，
    避免将来有人顺手把用户配置传进来。
    """
    conn.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


def table_exists(conn: Connection, table: str) -> bool:
    """表是否已经存在。"""
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).first()
    return row is not None


def columns_of(conn: Connection, table: str) -> set[str]:
    """取出一张表现有的列名。表不存在时返回空集合。"""
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def export_rows(conn: Connection, table: str) -> list[dict[str, Any]]:
    """把整张表读成 dict 列表，供重建后回灌。"""
    result = conn.exec_driver_sql(f"SELECT * FROM {table}")
    return [dict(row) for row in result.mappings()]


def import_rows(conn: Connection, model: Any, rows: list[dict[str, Any]]) -> None:
    """把 dict 列表写回 ``model`` 对应的表。

    只保留模型里存在的列：旧表可能带着后来被删掉的列，原样塞回去会报
    ``table has no column named ...``。被丢掉的列是新版本已经不要的字段，
    迁移函数有责任在此之前把有意义的值搬到新列上。
    """
    if not rows:
        return
    allowed = {column.name for column in model.__table__.columns}
    cleaned = [
        {key: value for key, value in row.items() if key in allowed} for row in rows
    ]
    conn.execute(model.__table__.insert(), cleaned)
