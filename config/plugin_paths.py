from pathlib import Path


class PluginPaths:
    """插件全部文件系统位置的唯一来源。

    命名约定：_dir  = 目录，_file  = 文件。
    拆开命名是因为对文件路径调用 mkdir 不会报错，
    而是在磁盘上造出同名目录，之后 open() 才抛 IsADirectoryError。
    """

    def __init__(self, data_path):
        # data路径改动的概率非常低
        self._data_path = Path(data_path)
        self.config_dir = self._data_path / "config"
        self.config_file = self._data_path / "config" / "config.json"
        self.db_dir = self._data_path / "db"
        self.sqlitedb_dir = self._data_path / "db" / "sqlite"
        # 目录只需创建一次，构造时完成，避免下游漏调用
        self.ensure_dirs()

    def ensure_dirs(self):
        """幂等创建所需目录。"""
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # db_path不需要再显式创建，在创建sqlitedb_path时会被递归创建
        self.sqlitedb_dir.mkdir(parents=True, exist_ok=True)
