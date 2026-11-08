from pathlib import Path
from sqlalchemy import Engine, event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
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

    def close(self) -> None:
        """关闭连接池，触发 WAL checkpoint。"""
        self._engine.dispose()