"""把 AstrBot 注入的插件配置解析成强类型对象。

AstrBot 依据 ``_conf_schema.json`` 生成的配置是 ``dict`` 子类，值可能缺失、
类型也可能被手改成字符串（用户在面板里填错）。本模块是唯一读取这些键的地方，
其它模块只拿解析好的对象，避免同一个键名散落在多处、改一处漏一处。

本模块不 import astrbot，便于在没有宿主的环境里测试。
"""

from dataclasses import dataclass
from typing import Mapping

# 服务端必填的 bank 标识；插件目前只用这一个库。
# 留空会拼出 ``/v1/default/banks//memories``，服务端一律 404，所以给个默认值。
DEFAULT_BANK_ID = "default"

# 与 _conf_schema.json 的 default 保持一致：配置项缺失时按这里的值兜底。
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 8888
_DEFAULT_BATCH_CHARS = 5000
_DEFAULT_RECALL_QUANTITY = 5
_DEFAULT_DAILY_TIME = "00:05"

# 上限只做保护：面板里滑块已经限死范围，但仍可能被手工改 JSON 绕过。
_MAX_BATCH_CHARS = 50000
_MAX_RECALL_QUANTITY = 50


def _as_int(value, default: int, *, minimum: int, maximum: int) -> int:
    """把配置值转成整数并夹到区间内。

    Args:
        value: 配置里的原始值。
        default: 转换失败时用的值。
        minimum: 允许的最小值（含）。
        maximum: 允许的最大值（含）。

    Returns:
        夹紧后的整数。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


@dataclass(frozen=True)
class HindsightConfig:
    """插件运行时需要的全部外部配置。

    Attributes:
        host: Hindsight 服务地址，可能已含 scheme 或端口。
        port: 服务端口。
        token: 鉴权 Token，空串表示服务端未开启鉴权。
        batch_chars: 单批提交的字符上限。
        bank_id: 记忆库标识。
        recall_quantity: 每次对话注入上下文的记忆条数。
        daily_time: 每日补投时刻，``HH:MM`` 形式。
    """

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    token: str = ""
    batch_chars: int = _DEFAULT_BATCH_CHARS
    bank_id: str = DEFAULT_BANK_ID
    recall_quantity: int = _DEFAULT_RECALL_QUANTITY
    daily_time: str = _DEFAULT_DAILY_TIME

    @classmethod
    def from_mapping(cls, config: Mapping | None) -> "HindsightConfig":
        """从 AstrBot 的配置字典构造。

        Args:
            config: 插件配置，通常是 ``dict`` 或 ``AstrBotConfig``；可为 None。

        Returns:
            解析后的配置对象；任何一项缺失或非法都退回默认值。
        """
        data = config or {}
        host = str(data.get("hindsight_host") or _DEFAULT_HOST).strip()
        token = str(data.get("hindsight_token") or "").strip()
        bank_id = str(data.get("hindsight_bank_id") or DEFAULT_BANK_ID).strip()
        return cls(
            host=host or _DEFAULT_HOST,
            port=_as_int(
                data.get("hindsight_port"), _DEFAULT_PORT, minimum=1, maximum=65535
            ),
            token=token,
            bank_id=bank_id or DEFAULT_BANK_ID,
            batch_chars=_as_int(
                data.get("sync_batch_chars"),
                _DEFAULT_BATCH_CHARS,
                minimum=1,
                maximum=_MAX_BATCH_CHARS,
            ),
            recall_quantity=_as_int(
                data.get("recall_memory_quantity"),
                _DEFAULT_RECALL_QUANTITY,
                minimum=1,
                maximum=_MAX_RECALL_QUANTITY,
            ),
            daily_time=str(data.get("sync_daily_time") or _DEFAULT_DAILY_TIME).strip()
            or _DEFAULT_DAILY_TIME,
        )
