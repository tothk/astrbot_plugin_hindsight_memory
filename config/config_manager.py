import os
import tempfile
from pathlib import Path

from astrbot.api import logger
import json
from pydantic import BaseModel, Field, ValidationError, ConfigDict

from .plugin_paths import PluginPaths


class Config(BaseModel):
    # 当传入的字典里有 Config 没定义的键时，直接扔掉，不报错。
    model_config = ConfigDict(extra="ignore")
    # 召回记忆数量,必须为正
    recall_memory_quantity: int = Field(default=5, ge=1)

    # hindsight连接配置
    hindsight_host: str = Field(default="localhost")
    hindsight_port: int = Field(default=8888)
    hindsight_api_key: str = Field(default="")


class ConfigManager:
    """config.json 的加载 / 校验 / 原子落盘。

        职责边界：只管一个 dict 与 Config 之间的转换，不碰数据库、不碰业务。
        任何配置异常都以“默认值 + 回写”收场，绝不上抛阻断插件启动。
    """

    def __init__(self, plugin_paths: PluginPaths):
        self._plugin_paths = plugin_paths
        # 先拿到完整实例再决定是否落盘，避免旧版
        # “_load 内部回调 save、而 self.config 尚未赋值”的半初始化状态
        self._config, need_persist = self._load()
        if need_persist:
            self.save()

    @property
    def config(self) -> Config:
        """只读视图；修改请走 update()，否则不会落盘。"""
        return self._config

    def _load(self) -> tuple[Config, bool]:
        """返回 (config, 是否需要回写磁盘)。"""
        try:
            with self._plugin_paths.config_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return Config(), True
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.warning("配置文件读取失败，使用默认值：%s (%s)", self._plugin_paths.config_file, exc)
            return Config(), True

        if not isinstance(raw, dict):
            logger.warning("配置文件根节点不是对象，使用默认值：%s", self._plugin_paths.config_file)
            return Config(), True

        try:
            return Config(**raw), False
        except ValidationError as exc:
            logger.warning("配置内容校验失败，使用默认值：%s", exc)
            return Config(), True

    def save(self) -> None:
        """原子写入。

        注意 mode="json"：默认 mode="python" 会原样返回 Path/datetime，
        json.dump 抛 "Object of type ... is not JSON serializable"。
        先写临时文件再 os.replace，避免中途失败留下半截 JSON 导致下次启动崩。
        """
        payload = self._config.model_dump(mode="json")
        target = self._plugin_paths.config_file

        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())  # 降低断电留空文件的概率
            os.replace(tmp_name, target)  # 同盘下为原子操作
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def update(self, **changes: object) -> None:
        """修改配置并立即落盘。

        用“合并 + 重新构造”而不是 setattr：直接改属性会绕过 pydantic 校验，
        非法值能一路带到下游（例如负数召回量变成诡异的切片结果）。
        """
        merged = {**self._config.model_dump(), **changes}
        self._config = Config(**merged)
        self.save()

    def reload(self) -> None:
        """从磁盘重新加载（手工编辑配置后调用）。"""
        self._config, need_persist = self._load()
        if need_persist:
            self.save()
