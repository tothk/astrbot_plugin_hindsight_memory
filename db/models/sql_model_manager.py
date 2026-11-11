from datetime import datetime
from typing import Sequence

from sqlalchemy import Engine, event, func, update
from sqlalchemy.exc import IntegrityError
# select 必须用 sqlmodel 的：它返回 SelectOfScalar，Session.exec 才会自动
# 调 .scalars() 取出模型对象。用 sqlalchemy.select 的话 exec 返回的是 Row，
# 访问 row.raw_message 会报 AttributeError。
from sqlmodel import Session, SQLModel, create_engine, select
from ...config.plugin_paths import PluginPaths
from ..migrations import run_migrations
from .models import (
    FLUSHED_DONE,
    FLUSHED_ENQUEUED,
    FLUSHED_PENDING,
    RawMessage,
    SyncOperation,
)
from .p_base_model import PBaseModel
class SqlModelManager:
    _DB_FILENAME = "raw_message.db"

    def __init__(self, plugin_paths: PluginPaths, log=None) -> None:
        """初始化引擎、挂 WAL 并建表。

        Args:
            plugin_paths: 提供 sqlitedb_dir 的路径对象。
            log: 可选的日志对象（提供 info/error），用于记录库文件位置与建表结果。
        """
        db_file = plugin_paths.sqlitedb_dir / self._DB_FILENAME
        self._log = log
        self._engine: Engine = create_engine(
            f"sqlite:///{db_file.as_posix()}",
            # astrbot 的钩子可能在不同线程里跑，关掉同线程检查
            # 抢锁最多等 10s
            connect_args={"check_same_thread": False, "timeout": 10}
        )

        @event.listens_for(self._engine, "connect")
        def _on_connect(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            # WAL 模式下写入先落在 -wal 文件，累积到 wal_autocheckpoint 页后
            # 才搬回主库。默认 1000 页（约 4MB），对一个只追加对话的小库来说
            # 太大：插件停用前主库可能一直是旧的，单拷 .db 会看不到最新记录。
            # 单位是「页」而不是字节，100 页约 400KB。
            cur.execute("PRAGMA wal_autocheckpoint=100")
            # autocheckpoint 只把内容搬回主库，-wal 文件本身不会缩小，
            # 会一直保持历史峰值。这条让它在搬完后截断到 1MB 以内。
            cur.execute("PRAGMA journal_size_limit=1048576")
            cur.close()
        # 结构升级必须排在 create_all 之前：老库要先按版本链爬上来，
        # create_all 才不会用它那套「只建不补」的逻辑把老表晾在那儿。
        run_migrations(self._engine, log=self._log)
        PBaseModel.metadata.create_all(self._engine)
        # create_all 只建新表，不会给已存在的表补列。开发期表结构改过几次，
        # 用户的库里可能少了后来加的列（例如 sender_name），届时任何查询都会
        # 报 "no such column"。这里做一次最小的补齐，不改类型、不删列。
        # 注意：迁移链只处理「有版本号变化」的库，版本号已经是当前值但结构
        # 落后（版本化之前手工改过表）的库仍由这里兜底。
        self._ensure_columns()
        # 实测期间最常需要确认的就是"库到底落在哪"，启动时打一次即可。
        if self._log is not None:
            self._log.info(f"原始对话库位置: {db_file}")

    def _ensure_columns(self) -> None:
        """给已存在的表补上新增的可空列。

        只处理「新加的、有默认值的列」这一种情况，够用且绝不破坏数据。
        真要改类型或删列，还是得手工迁移。
        """
        expected = {"sender_name": "TEXT DEFAULT ''"}
        with self._engine.begin() as conn:
            existing = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(rawmessage)")
            }
            for column, ddl in expected.items():
                if column in existing:
                    continue
                conn.exec_driver_sql(
                    f"ALTER TABLE rawmessage ADD COLUMN {column} {ddl}"
                )
                # 补列属于结构变更，值得留一条日志，便于排查"为什么突然能写了"。
                if self._log is not None:
                    self._log.info(f"数据库补列: rawmessage.{column}")

    def insert_raw(self, raw: RawMessage) -> RawMessage | None:
        """插入一条原始消息，返回带 id 的对象。

        content_hash 上有唯一索引，同一份内容重复写入会触发 IntegrityError。
        这里把它当作"已经记过了"吞掉并返回 None，而不是向上抛——重复不是故障，
        不能因此给用户发"保存失败"的提示。

        Args:
            raw: 待插入的记录。

        Returns:
            插入成功返回带 id 的对象；内容重复已存在时返回 None。
        """
        try:
            with Session(self._engine) as session:
                session.add(raw)
                session.commit()
                session.refresh(raw)
                return raw
        except IntegrityError:
            # 唯一索引冲突：这条内容此前已经记录过，视为成功。
            return None

    def list_unflushed(self, limit: int = 1000) -> list[RawMessage]:
        """按 id 升序取出尚未提交给 Hindsight 的记录。

        用 id 作为游标而不是时间：id 严格递增且不受系统时钟回拨影响，
        「上次同步到哪」可以直接由「最后一条 flushed 不为 PENDING 的 id」推出。

        只取 ``FLUSHED_PENDING``：``FLUSHED_ENQUEUED`` 的行已经提交过、
        正在等服务端确认，再投一次会在文档末尾重复追加。

        Args:
            limit: 最多取多少条，防止积压过多时一次性读进内存。

        Returns:
            尚未提交的记录列表，按 id 升序；没有则返回空列表。
        """
        statement = (
            select(RawMessage)
            .where(RawMessage.flushed == FLUSHED_PENDING)
            .order_by(RawMessage.id)
            .limit(limit)
        )
        with Session(self._engine) as session:
            return list(session.exec(statement))

    def mark_flushed(self, ids: Sequence[int], value: int = FLUSHED_DONE) -> int:
        """把给定记录标记为指定状态。

        默认标记为「服务端已确认完成」。仅想让记录变成「已入队」时请用
        ``mark_enqueued()``——它会同时写下操作凭据，便于日后对账。

        Args:
            ids: 记录主键列表；空列表会直接返回 0，不执行 SQL。
            value: 目标状态，取 ``FLUSHED_PENDING`` / ``FLUSHED_ENQUEUED``
                / ``FLUSHED_DONE``。

        Returns:
            实际更新的行数。
        """
        if not ids:
            return 0
        statement = (
            update(RawMessage)
            .where(RawMessage.id.in_(list(ids)))
            .values(flushed=value)
        )
        with Session(self._engine) as session:
            # 用 execute 而不是 exec：exec 只接受 select，Update 语句会报类型错误。
            result = session.execute(statement)
            session.commit()
            return result.rowcount or 0

    def mark_enqueued(
        self,
        ids: Sequence[int],
        *,
        operation_id: str,
        batch_key: str = "",
        items_count: int = 0,
        error: str = "",
    ) -> int:
        """把一批记录标记为「已入队」，并写下对账所需的操作凭据。

        这两件事必须一起做，所以放在同一个事务里：只标 ``flushed`` 而不记
        ``operation_id``，日后就无从知道该向服务端问哪一批；只记凭据而不标
        ``flushed``，下一轮会把同一批内容再投一次。

        Args:
            ids: 本批覆盖的记录主键，必须非空。
            operation_id: 服务端返回的操作标识（父操作）。
            batch_key: 本地批次标识，仅用于日志与排错。
            items_count: 本批提交的 item 数量。
            error: 入队阶段通常为空，预留接口。

        Returns:
            实际更新为「已入队」的行数；ids 为空时返回 0 且不写记录。
        """
        id_list = [int(i) for i in ids if i is not None]
        if not id_list:
            return 0

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        record = SyncOperation(
            operation_id=operation_id,
            first_row_id=min(id_list),
            last_row_id=max(id_list),
            items_count=items_count,
            status="pending",
            error=error,
            created_at=now,
            updated_at=now,
        )
        with Session(self._engine) as session:
            session.add(record)
            statement = (
                update(RawMessage)
                .where(RawMessage.id.in_(id_list))
                .values(flushed=FLUSHED_ENQUEUED)
            )
            result = session.execute(statement)
            session.commit()
            updated = result.rowcount or 0

        if self._log is not None:
            self._log.info(
                f"已入队 {updated} 条记录 → operation={operation_id} batch={batch_key or '-'}"
            )
        return updated

    def pending_operations(self, limit: int = 100) -> list[SyncOperation]:
        """取出还没有结论的提交批次，按创建顺序。

        包含 ``failed`` 的批次：它们还没被解决，下一轮巡检要接着调 retry。
        只排除 ``completed``——那才是真正的终点，记录也已在完成时删掉。
        如果这里过滤掉 ``failed``，一个失败批次就会从此无人问津，而那正是
        这张表要防止的事。

        巡检要按「先提交的先查」处理，避免某一批长期排不上队。

        Args:
            limit: 最多取多少条。

        Returns:
            状态为 ``pending`` 或 ``failed`` 的批次列表。
        """
        statement = (
            select(SyncOperation)
            .where(SyncOperation.status != "completed")
            .order_by(SyncOperation.id)
            .limit(limit)
        )
        with Session(self._engine) as session:
            return list(session.exec(statement))

    def resolve_operation(
        self,
        operation_id: str,
        *,
        status: str,
        error: str = "",
        attempts: int | None = None,
    ) -> int:
        """记录某个批次的处理结论，并按结论改写这批记录的状态。

        ``completed`` 会把区间内的记录标成 ``FLUSHED_DONE`` 并删掉批次记录
        ——对账的目的就是让这里不再需要被查到。``failed`` 只更新批次记录，
        记录仍停在 ``FLUSHED_ENQUEUED``，等 retry 或最终回退时再动。

        Args:
            operation_id: 批次的父操作标识。
            status: 结论，``completed`` 或 ``failed``。
            error: 失败原因，写入批次记录便于排查。
            attempts: 见过的失败次数；不传则不动。

        Returns:
            completed 时返回被标记为完成的记录行数；failed 时返回被更新的
            批次记录行数；找不到该批次返回 0。
        """
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with Session(self._engine) as session:
            statement = select(SyncOperation).where(
                SyncOperation.operation_id == operation_id
            )
            record = session.exec(statement).first()
            if record is None:
                return 0

            if status == "completed":
                # 先记下 id 区间，删记录之后再也读不到。
                first_id, last_id = record.first_row_id, record.last_row_id
                session.delete(record)
                result = session.execute(
                    update(RawMessage)
                    .where(
                        RawMessage.id >= first_id,
                        RawMessage.id <= last_id,
                        RawMessage.flushed == FLUSHED_ENQUEUED,
                    )
                    .values(flushed=FLUSHED_DONE)
                )
                session.commit()
                return result.rowcount or 0

            record.status = status
            record.error = error
            record.updated_at = now
            if attempts is not None:
                record.attempts = attempts
            session.add(record)
            session.commit()
            return 1

    def release_operation(self, operation_id: str) -> int:
        """撤回一个批次：把记录退回待投递，并删掉批次凭据。

        用于「服务端已经彻底失败、重试也救不回来」的情况。下轮同步会把
        这些记录重新投一次——接受内容在文档末尾重复追加的代价，总比永久
        丢数据好。

        Args:
            operation_id: 批次的父操作标识。

        Returns:
            被退回 ``FLUSHED_PENDING`` 的行数。
        """
        with Session(self._engine) as session:
            statement = select(SyncOperation).where(
                SyncOperation.operation_id == operation_id
            )
            record = session.exec(statement).first()
            if record is None:
                return 0
            first_id, last_id = record.first_row_id, record.last_row_id
            session.delete(record)
            result = session.execute(
                update(RawMessage)
                .where(
                    RawMessage.id >= first_id,
                    RawMessage.id <= last_id,
                    RawMessage.flushed == FLUSHED_ENQUEUED,
                )
                .values(flushed=FLUSHED_PENDING)
            )
            session.commit()
            released = result.rowcount or 0

        if self._log is not None and released:
            self._log.warning(
                f"批次 {operation_id} 已撤回，{released} 条记录退回待投递"
            )
        return released

    def count_by_flushed(self) -> dict[int, int]:
        """按投递状态统计记录数，用于日志与状态展示。

        Returns:
            形如 ``{0: 5, 1: 2, 2: 30}`` 的字典，只包含出现过的状态。
        """
        statement = select(RawMessage.flushed, func.count()).group_by(RawMessage.flushed)
        with Session(self._engine) as session:
            return {int(state): int(count) for state, count in session.exec(statement)}

    def close(self) -> None:
        """关闭连接池，触发 WAL checkpoint。"""
        self._engine.dispose()