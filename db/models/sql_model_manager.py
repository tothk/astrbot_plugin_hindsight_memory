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

    def _existing_hashes(self, hashes: Sequence[str]) -> set[str]:
        """查出这些 content_hash 里已经存在于库中的那些。

        Args:
            hashes: 待检查的哈希列表，调用方需保证非空。

        Returns:
            已存在的哈希集合。
        """
        statement = select(RawMessage.content_hash).where(
            RawMessage.content_hash.in_(list(hashes))
        )
        with Session(self._engine) as session:
            return set(session.exec(statement))

    def insert_many(self, rows: Sequence[RawMessage]) -> list[RawMessage]:
        """在同一个事务里写入多条记录，返回真正新增的行。

        不能简单 add_all + commit：``content_hash`` 上有唯一索引，只要有一条
        重复，整批 commit 都会抛 IntegrityError 并回滚，新内容会跟着一起丢。
        所以先把重复的筛掉（库中已有的 + 批内自重复的），剩下的放进一个事务。

        Args:
            rows: 待插入的记录列表。

        Returns:
            真正新增的记录，已带主键，顺序与传入顺序一致；全部重复时为空列表。

        Note:
            筛选与插入之间仍有窗口：另一个进程可能恰好插入了同样的内容。
            那种情况 commit 会抛 IntegrityError，这里退回逐条插入，
            至少保住不重复的那部分——批量在单进程下是常态，这个兜底
            几乎不会走到，但走到时不能整批丢。
        """
        if not rows:
            return []

        wanted = list(dict.fromkeys(raw.content_hash for raw in rows))
        existing = self._existing_hashes(wanted)

        fresh: list[RawMessage] = []
        seen: set[str] = set()
        for raw in rows:
            key = raw.content_hash
            if key in existing or key in seen:
                continue
            seen.add(key)
            fresh.append(raw)

        if not fresh:
            return []

        try:
            with Session(self._engine, expire_on_commit=False) as session:
                session.add_all(fresh)
                session.commit()
            return fresh
        except IntegrityError:
            # 竞态：筛选之后有人抢先插了同样的内容。退回逐条写，能写几条算几条。
            inserted: list[RawMessage] = []
            for raw in fresh:
                saved = self.insert_raw(raw)
                if saved is not None:
                    inserted.append(saved)
            return inserted

    def list_unflushed(self, limit: int = 1000) -> list[RawMessage]:
        """按 id 升序取出尚未提交给 Hindsight 的记录。

        用 id 作为游标而不是时间：id 严格递增且不受系统时钟回拨影响，
        「上次同步到哪」可以直接由「最后一条 flushed 不为 PENDING 的 id」推出。

        只取 ``FLUSHED_PENDING``：``FLUSHED_ENQUEUED`` 的行已经提交过、
        正在等服务端确认，再投一次会在文档末尾重复追加。

        Args:
            limit: 最多取多少条，防止积压过多时一次性读进内存。默认值与
                ``hindsight.sync._DEFAULT_PENDING_LIMIT`` 保持一致：这个
                默认值只是给直接调用留的方便，正常路径由同步层显式传入。

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
        error: str = "",
    ) -> list[int]:
        """认领一批记录（待投递 → 已入队），并写下对账所需的操作凭据。

        只认领仍处于「待投递」的记录：``WHERE flushed = FLUSHED_PENDING``
        与状态变更是同一条 UPDATE 完成的，所以在语句层面就是原子的。这一点
        比进程内的锁更强——别的 ``sync_once``、另一个 AstrBot 实例、乃至手工
        打开的 sqlite 客户端，都拿不到同一批行的写锁。

        为什么必须这么判：调用方（以及主动写入那条路径）是「先写库、后获锁
        再投递」，两者之间敞着一个窗口，每日补投、手动指令、别的会话的积压
        触发都可能在这个窗口里把这批行扫走并投出去。如果这里无条件更新，
        后到的调用方会把已被投递的行再投一遍（``append`` 语义下内容重复），
        甚至把已经落成「已完成」的行改回「已入队」，造出一个永远等不到对账
        的中间态。

        Args:
            ids: 本批候选的记录主键，必须非空。
            operation_id: 服务端返回的操作标识（父操作）。
            batch_key: 本地批次标识，仅用于日志与排错。
            error: 入队阶段通常为空，预留接口。

        Returns:
            实际认领到的主键列表，按传入顺序；一个都没认领到时为空列表，
            此时**不会**写下 ``SyncOperation`` 记录——凭据表描述的是「这一次
            到底提交了什么」，没提交就不该留痕。
        """
        wanted = [int(i) for i in ids if i is not None]
        if not wanted:
            return []

        with Session(self._engine) as session:
            statement = (
                update(RawMessage)
                .where(
                    RawMessage.id.in_(wanted),
                    RawMessage.flushed == FLUSHED_PENDING,
                )
                .values(
                    flushed=FLUSHED_ENQUEUED,
                    # 归属与状态在同一条 UPDATE 里写下：对账时按归属回写，
                    # 不必再靠 id 区间反推（区间会串到别的批次）。
                    operation_id=operation_id,
                )
                # RETURNING 让「改了哪几行」和「改成了什么」出自同一条语句，
                # 不必再查一次（再查一次就又有了窗口）。
                .returning(RawMessage.id)
            )
            claimed = [row[0] for row in session.execute(statement).fetchall()]
            if not claimed:
                session.commit()
                return []

            # 保持传入顺序，方便调用方按认领结果过滤原批次。
            claimed_set = set(claimed)
            ordered = [i for i in wanted if i in claimed_set]

            now = datetime.now().astimezone().isoformat(timespec="seconds")
            session.add(
                SyncOperation(
                    operation_id=operation_id,
                    # 区间取实际认领到的行，可能窄于原批次。仅用于日志与排错，
                    # 归属判断一律走 RawMessage.operation_id。
                    first_row_id=min(ordered),
                    last_row_id=max(ordered),
                    # 记「实际认领到的行数」而不是调用方给的 items_count：
                    # 部分认领是正常路径（同一批被别的同步轮次抢先拿走一部分），
                    # 按调用方的数字记，对账时会把这种正常情况误报成
                    # 「回写行数与认领时不一致」。
                    items_count=len(ordered),
                    status="pending",
                    error=error,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        if self._log is not None:
            skipped = len(wanted) - len(ordered)
            suffix = f"，跳过 {skipped} 条已被其它同步轮次认领" if skipped else ""
            self._log.info(
                f"已认领 {len(ordered)} 条记录 → operation={operation_id} "
                f"batch={batch_key or '-'}{suffix}"
            )
        return ordered

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

        ``completed`` 会把归属该批次的记录标成 ``FLUSHED_DONE`` 并删掉批次
        记录——对账的目的就是让这里不再需要被查到。``failed`` 只更新批次记录，
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
                # 认领时记下了多少行，必须在删记录前读出来：commit 之后这个
                # 对象已经过期，再取字段会抛 ObjectDeletedError。
                expected = record.items_count
                session.delete(record)
                # 按归属回写，不按 id 区间：区间是闭集，认领带洞时会盖住
                # 别的批次仍处于「已入队」的行，把它们的记忆无凭无据地
                # 标成已完成，绕过 retry/release。
                result = session.execute(
                    update(RawMessage)
                    .where(
                        RawMessage.operation_id == operation_id,
                        RawMessage.flushed == FLUSHED_ENQUEUED,
                    )
                    # 归属在完成时清空：这行已经落地，不再属于任何在途批次。
                    .values(flushed=FLUSHED_DONE, operation_id="")
                )
                session.commit()
                resolved = result.rowcount or 0
                if self._log is not None and expected and resolved != expected:
                    # 行数少了说明本批有行已不在「已入队」（被别的路径动过，
                    # 或迁移时被退回）。不自动补，只留痕便于排查。
                    self._log.warning(
                        f"批次 {operation_id} 回写了 {resolved} 行，"
                        f"与认领时的 {expected} 行不一致"
                    )
                return resolved

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
            session.delete(record)
            # 与 resolve_operation 对称：按归属回写，并清空归属。
            result = session.execute(
                update(RawMessage)
                .where(
                    RawMessage.operation_id == operation_id,
                    RawMessage.flushed == FLUSHED_ENQUEUED,
                )
                .values(flushed=FLUSHED_PENDING, operation_id="")
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