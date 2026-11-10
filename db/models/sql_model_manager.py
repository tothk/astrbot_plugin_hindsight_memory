from typing import Sequence

from sqlalchemy import Engine, event, update
from sqlalchemy.exc import IntegrityError
# select 必须用 sqlmodel 的：它返回 SelectOfScalar，Session.exec 才会自动
# 调 .scalars() 取出模型对象。用 sqlalchemy.select 的话 exec 返回的是 Row，
# 访问 row.raw_message 会报 AttributeError。
from sqlmodel import Session, SQLModel, create_engine, select
from ...config.plugin_paths import PluginPaths
from .models import RawMessage
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
        PBaseModel.metadata.create_all(self._engine)
        # 实测期间最常需要确认的就是"库到底落在哪"，启动时打一次即可。
        if self._log is not None:
            self._log.info(f"原始对话库位置: {db_file}")

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
        「上次同步到哪」可以直接由「最后一条 flushed=1 的 id」推出。

        Args:
            limit: 最多取多少条，防止积压过多时一次性读进内存。

        Returns:
            尚未提交的记录列表，按 id 升序；没有则返回空列表。
        """
        statement = (
            select(RawMessage)
            .where(RawMessage.flushed == 0)
            .order_by(RawMessage.id)
            .limit(limit)
        )
        with Session(self._engine) as session:
            return list(session.exec(statement))

    def mark_flushed(self, ids: Sequence[int]) -> int:
        """把给定记录标记为已提交。

        Args:
            ids: 记录主键列表；空列表会直接返回 0，不执行 SQL。

        Returns:
            实际更新的行数。
        """
        if not ids:
            return 0
        statement = (
            update(RawMessage)
            .where(RawMessage.id.in_(list(ids)))
            .values(flushed=1)
        )
        with Session(self._engine) as session:
            # 用 execute 而不是 exec：exec 只接受 select，Update 语句会报类型错误。
            result = session.execute(statement)
            session.commit()
            return result.rowcount or 0

    def close(self) -> None:
        """关闭连接池，触发 WAL checkpoint。"""
        self._engine.dispose()