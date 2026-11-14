"""数据库结构迁移。

每个版本一步，按 ``MIGRATIONS`` 的顺序往上爬，全部走完才允许插件继续启动。
版本号存在 SQLite 的 ``PRAGMA user_version`` 里，不占表、不依赖任何表结构。

为什么不用 Alembic：它面向多分支、可回滚、多人协作的场景，代价是一整套
``versions/`` 目录和配置文件，还会给插件添一个运行时依赖。这里两张表、
单人开发、只往前升级，``user_version`` 加一个 dict 就是它的完整替代。

版本历史：
- v1：初始结构（rawmessage + syncoperation，批次归属用 id 区间表示）。
- v2：批次归属落到 ``rawmessage.operation_id`` 上，见 :mod:`v1_to_v2`。
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
from .v1_to_v2 import sql_v1_to_v2

# 当前代码期望的结构版本。加新版本时改这里，并在 MIGRATIONS 里补一步。
CURRENT_VERSION = 2

# key = 目标版本。值是「把库从 key-1 带到 key」的那一步。
MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: sql_v1_to_v2,
}

# 版本化之前建的库（user_version 一直是 0）统一认作这个版本。
# 必须写死 1 而不是 CURRENT_VERSION：老库的真实形状只有 v1 一种，把它直接
# 盖成最新版会让 v1→v2 这一步被跳过——列没加、索引没建，启动时才炸在
# 「no such column: operation_id」，那时用户的数据已经在库里了。
BASELINE_VERSION = 1


def _detect_baseline(conn: Connection, log=None) -> int:
    """确定版本化之前创建的库停在哪个版本。

    版本号是后加的，老库的 ``user_version`` 都是 0。版本化引入时 v0.0.1 的
    结构就是 v1，所以「有 rawmessage 表」一律按 v1 处理，之后由迁移链正常
    往上爬。

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
        # 全新库：表由 create_all 按当前模型建（已经是最新形状），直接标到
        # 当前版本，不必再走迁移链。
        set_user_version(conn, CURRENT_VERSION)
        return CURRENT_VERSION

    # 有 rawmessage 但没有版本号：版本化之前建的库，按 v1 处理。
    if "content_hash" not in columns_of(conn, "rawmessage"):
        raise RuntimeError(
            "无法识别现有数据库的结构版本（rawmessage 存在但没有 content_hash），"
            "请备份 db/sqlite/raw_message.db 后联系开发者。"
        )

    if log is not None:
        log.warning(f"检测到版本化之前创建的数据库，标记为 v{BASELINE_VERSION}")
    set_user_version(conn, BASELINE_VERSION)
    return BASELINE_VERSION


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
