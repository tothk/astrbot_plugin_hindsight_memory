"""v1 → v2：把批次的归属从「id 区间」搬到「行上的 operation_id」。

v1 用 ``syncoperation.first_row_id / last_row_id`` 这个闭区间来表示「哪些行
属于这一批」，回写时按 ``id BETWEEN first AND last`` 改状态。区间不是集合：
一次认领拿到的 id 可以带洞（低 id 的行被上一个失败批次退回过，中间又插进了
新行），此时区间会盖住别的批次仍处于「已入队」的行——某批对账完成时把别人的
行一起标成完成，绕过 retry/release，那部分记忆就静默丢了。

v2 在 ``rawmessage`` 上加一列 ``operation_id``，认领、对账、撤回全部改成
``WHERE operation_id = ?``，判据从「范围」变成「归属」。

在途批次怎么办
--------------
升级时可能存在「已入队、还没对账」的批次。它们的行在加列之后
``operation_id`` 是空串，``WHERE operation_id = ?`` 一条也匹配不到，
对账完成时会删掉凭据、行却永远停在 FLUSHED_ENQUEUED——比原来的 bug 更糟。

这里选择**全部退回待投递**，而不是按 v1 的 id 区间回填归属：回填等于把
「区间即归属」这个错误假设再赌一次，赌错就是静默丢记忆；退回只是让在途的
几批重投一遍，代价是文档末尾可能多一段重复内容。重复可以接受，丢失不行。
"""

from sqlalchemy.engine import Connection

from .sql_utils import columns_of, table_exists

# v1 里 FLUSHED_ENQUEUED / FLUSHED_PENDING 的字面值。迁移不能 import 模型
# 常量：模型描述的是当前形状，将来这些值再变时，历史迁移必须是冻结的。
_ENQUEUED = 1
_PENDING = 0


def sql_v1_to_v2(conn: Connection) -> None:
    """给 rawmessage 加 operation_id，并把在途批次退回待投递。

    Args:
        conn: 已打开的连接，调用方负责把它包在事务里。

    Note:
        幂等：先看列在不在。写成幂等是为了让「迁移跑到一半被中断」这种情况
        可以重跑，哪怕版本号没跟着提交。
    """
    if not table_exists(conn, "rawmessage"):
        # 走到这里说明库的形状和版本号对不上（版本号是 1，却没有 rawmessage）。
        # 直接跳过结构变更，交给 create_all 按当前模型建表。
        return

    if "operation_id" not in columns_of(conn, "rawmessage"):
        # SQLite 的 ALTER TABLE ADD COLUMN 不能同时建索引，索引单独补。
        conn.exec_driver_sql(
            "ALTER TABLE rawmessage ADD COLUMN operation_id TEXT NOT NULL DEFAULT ''"
        )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_rawmessage_operation_id "
        "ON rawmessage (operation_id)"
    )

    # 在途批次整体退回待投递：加列之后它们的归属信息已经不存在了。
    conn.exec_driver_sql(
        f"UPDATE rawmessage SET flushed = {_PENDING} WHERE flushed = {_ENQUEUED}"
    )
    # 凭据一并清掉：它们描述的区间已经失效，留着会让对账去查一批谁也不属于
    # 的操作，然后把「0 行」当成功报出来。
    if table_exists(conn, "syncoperation"):
        conn.exec_driver_sql("DELETE FROM syncoperation")
