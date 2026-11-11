"""数据库结构迁移。

每个版本一步，按 ``MIGRATIONS`` 的顺序往上爬，全部走完才允许插件继续启动。
版本号存在 SQLite 的 ``PRAGMA user_version`` 里，不占表、不依赖任何表结构。

当前没有待执行的迁移：v0.0.1 的表结构就是 v1，也是代码期望的版本。这套机制
现在就做一件事——给已经存在的库打上 ``user_version = 1``，让「这个库是第几版
结构」这个信息第一次真正存在。下次要加列、改类型、拆表时，写一个
``v1_to_v2.py`` 并把 ``CURRENT_VERSION`` 改成 2 即可，老库会自动往上爬。

为什么不用 Alembic：它面向多分支、可回滚、多人协作的场景，代价是一整套
``versions/`` 目录和配置文件，还会给插件添一个运行时依赖。这里两张表、
单人开发、只往前升级，``user_version`` 加一个 dict 就是它的完整替代。
"""

from typing import Callable

from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from .sql_utils import (
    columns_of,
    get_user_version,
    set_user_version,
    table_exists,
)

# 当前代码期望的结构版本。加新版本时改这里，并在 MIGRATIONS 里补一步。
CURRENT_VERSION = 1

# key = 目标版本。值是「把库从 key-1 带到 key」的那一步。
# 现在为空：v1 就是初始结构，没有需要转换的老版本。
MIGRATIONS: dict[int, Callable[[Connection], None]] = {}


def _detect_baseline(conn: Connection, log=None) -> int:
    """确定版本化之前创建的库停在哪个版本。

    版本号是后加的，老库的 ``user_version`` 都是 0。v0.0.1 的表结构就是 v1，
    所以「有 rawmessage 表」一律按 v1 处理，不需要再区分更细的形状。

    比 v1 还老的开发期库（没有 content_hash）直接抛异常让启动失败：那种库
    缺的不止一列，猜着补会把结构越补越乱，不如让人看见。宁可启动不起来，
    也不要写坏用户仅有的原始记录。

    Args:
        conn: 已打开的连接。
        log: 可选的日志对象。

    Returns:
        识别出的当前版本号。

    Raises:
        RuntimeError: 库里有 rawmessage 表，但结构比 v1 还老。
    """
    version = get_user_version(conn)
    if version > 0:
        return version

    if not table_exists(conn, "rawmessage"):
        # 全新库：表由 create_all 按当前模型建，直接标到当前版本。
        set_user_version(conn, CURRENT_VERSION)
        return CURRENT_VERSION

    # 有 rawmessage 但没有版本号：版本化之前建的库，按 v1 处理。
    if "content_hash" not in columns_of(conn, "rawmessage"):
        raise RuntimeError(
            "无法识别现有数据库的结构版本（rawmessage 存在但没有 content_hash），"
            "请备份 db/sqlite/raw_message.db 后联系开发者。"
        )

    if log is not None:
        log.warning(f"检测到版本化之前创建的数据库，标记为 v{CURRENT_VERSION}")
    set_user_version(conn, CURRENT_VERSION)
    return CURRENT_VERSION


def run_migrations(engine: Engine, log=None) -> int:
    """把数据库升到 ``CURRENT_VERSION``。

    Args:
        engine: 指向本插件 sqlite 文件的引擎。
        log: 可选的日志对象（提供 info/warning），记录每一步的进展。

    Returns:
        迁移结束后的版本号（等于 ``CURRENT_VERSION``）。

    Raises:
        RuntimeError: 库版本高于插件支持的版本，或无法识别老库版本。
    """
    # 用 begin() 而不是 connect()：_detect_baseline 会顺手把版本号写进去，
    # connect() 开的是隐式事务，关闭时不提交，写入会悄悄丢掉。
    with engine.begin() as conn:
        version = _detect_baseline(conn, log)

    if version > CURRENT_VERSION:
        raise RuntimeError(
            f"数据库结构版本 v{version} 高于插件支持的 v{CURRENT_VERSION}，"
            "可能是插件被降级了。请升级插件，或从备份恢复数据库。"
        )

    with engine.connect() as conn:
        while version < CURRENT_VERSION:
            target = version + 1
            step = MIGRATIONS[target]
            if log is not None:
                log.info(f"数据库迁移 v{version} → v{target} 开始（{step.__name__}）")
            # 版本号与结构变更写在同一个事务里：失败就一起回滚，
            # 不会留下「结构改了但版本没改」的半截状态。
            with conn.begin():
                step(conn)
                set_user_version(conn, target)
            if log is not None:
                log.info(f"数据库迁移 v{target} 完成")
            version = target

    return version
