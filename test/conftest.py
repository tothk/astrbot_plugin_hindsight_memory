"""pytest 的公共夹具与导入准备。

插件内部用的是相对导入（例如 ``from ..db.models.models import RawMessage``），
所以测试必须让插件作为一个包被导入。这里把插件目录的上一级
（``data/plugins``）加进 sys.path，测试里就可以写

    import astrbot_plugin_hindsight_memory.core.recorder

包名与 AstrBot 实际加载时的 ``data.plugins.<目录名>`` 不同，但相对导入只关心
包的层级结构，这一点不影响被测代码的行为。
"""

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = PLUGIN_DIR.parents[2]
# 测试用的临时目录根。放在插件目录里而不是系统临时目录，见 workdir 夹具的说明。
TMP_ROOT = PLUGIN_DIR / ".test-tmp"

# 必须在 import astrbot 之前设好：astrbot/core/__init__.py 一被导入就会
# makedirs(get_astrbot_data_path())，并写 cmd_config.json、拷 t2i 模板。
# 不设 ASTRBOT_ROOT 时它退回 os.getcwd()，于是插件目录里会凭空多出一个
# data/（里面还有 dashboard 的默认凭据）。这里把它指到测试专用目录，
# 跑测试就不会污染插件目录，也不会碰真实的 AstrBot 数据。
os.environ["ASTRBOT_ROOT"] = str(TMP_ROOT / "astrbot_root")
os.makedirs(os.environ["ASTRBOT_ROOT"], exist_ok=True)

for _entry in (str(ASTRBOT_ROOT), str(PLUGIN_DIR.parent)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from astrbot_plugin_hindsight_memory.config.plugin_paths import PluginPaths  # noqa: E402
from astrbot_plugin_hindsight_memory.db.models.sql_model_manager import (  # noqa: E402
    SqlModelManager,
)


class ListLog:
    """把日志收进列表的假 logger。

    core/recorder.py 的 Logger 协议只要求 debug/info/error 三个方法，
    这里实现同样的形状，用来断言"打了什么级别的日志、内容对不对"。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def debug(self, msg: str) -> None:
        self.messages.append(("debug", msg))

    def info(self, msg: str) -> None:
        self.messages.append(("info", msg))

    def error(self, msg: str) -> None:
        self.messages.append(("error", msg))

    def text_of(self, level: str) -> list[str]:
        """取指定级别的全部日志正文。"""
        return [msg for lvl, msg in self.messages if lvl == level]


class BoomSql:
    """每次插入都失败的假数据库，用来模拟抢不到写锁等故障。"""

    def __init__(self, message: str = "database is locked") -> None:
        self.message = message
        self.calls = 0

    def insert_raw(self, raw):
        self.calls += 1
        raise RuntimeError(self.message)


@pytest.fixture
def workdir() -> Path:
    """给单个测试用的干净目录，测完删掉。

    不用 pytest 内置的 tmp_path：它建目录时带 mode=0o700，而在本机这个
    工作区里，带 0o700 建出来的目录再往里 mkdir 会报
    ``PermissionError: [WinError 5]``（pytest 会报成
    "could not create numbered dir ... after 20 tries"）。
    这里用默认权限建目录，避开这个坑。
    """
    base = TMP_ROOT / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def paths(workdir: Path) -> PluginPaths:
    """落在临时目录里的路径对象。"""
    return PluginPaths(workdir / "plugin_data")


@pytest.fixture
def log() -> ListLog:
    """一套干净的假 logger。"""
    return ListLog()


@pytest.fixture
def sql(paths: PluginPaths):
    """连到临时库的 SqlModelManager，测试结束关闭引擎。"""
    manager = SqlModelManager(paths)
    yield manager
    manager.close()
