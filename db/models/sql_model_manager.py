from pathlib import Path
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine
from ...config.plugin_paths import PluginPaths
from .models import RawMessage
from .p_base_model import PBaseModel
class SqlModelManager:
    _DB_FILENAME = "raw_message.db"

    def __init__(self, plugin_paths: PluginPaths) -> None:
        db_file = plugin_paths.sqlitedb_dir / self._DB_FILENAME
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

    def insert_raw(self, raw: RawMessage) -> RawMessage:
        """插入一条原始消息，返回带 id 的对象。"""
        with Session(self._engine) as session:
            session.add(raw)
            session.commit()
            session.refresh(raw)
            return raw
    def close(self) -> None:
        """关闭连接池，触发 WAL checkpoint。"""
        self._engine.dispose()