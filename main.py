from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .config.plugin_paths import PluginPaths
from .db.models.sql_model_manager import SqlModelManager
from .core.capture import MessageCapture
from .core.recorder import Recorder


# 插件元数据由 metadata.yaml 提供。AstrBot 3.5.19+ 会自动识别继承 Star 的插件类，
# 已废弃的 @register 装饰器不再使用，避免宿主未来移除它时插件无法加载。
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        """初始化插件。

        Args:
            context: AstrBot 注入的上下文。
            config: AstrBot 依据 _conf_schema.json 生成的插件配置。
                开发期统一以它为唯一数据源；插件自己持有的 config.json
                暂不使用（后续接 WebUI 时再恢复）。
        """
        super().__init__(context)

        # 面板里编辑的值最终落在这里；缺省时 Star 基类已给了空 dict。
        self._config: dict = config or {}
        self.PLUGIN_DATA_PATH = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self._plugin_paths = PluginPaths(self.PLUGIN_DATA_PATH)
        # self.logger 是 AstrBot 为本插件单独准备的 logger（面板里可单独调级别），
        # 比模块级的 astrbot 全局 logger 更适合记录运行期信息。
        self._sql = SqlModelManager(self._plugin_paths, self.logger)
        self._recorder = Recorder(self._sql, self.logger)
        self._capture = MessageCapture(
            self.context, self._recorder, self._notify_save_failed, self.logger
        )

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 用插件专属 logger：面板里可以单独调它的级别，不会淹没在全局日志里。
        self.logger.info("Hindsight 记忆储存插件已加载，开始记录原始对话。")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

        # 插件卸载/停用时的清理。
        self._sql.close()
        self.logger.info("Hindsight 记忆储存插件已停止记录。")

    async def _notify_save_failed(self, event: AstrMessageEvent):
        """记录写入失败时提示用户，避免记忆静默缺失。

        必须走 context.send_message 而不是 event.send：后者会把
        _has_send_oper 置为 True，导致本轮的正式回复被这条提示顶掉。
        节流（只在首次失败时提示）由 Recorder 内部负责。

        Args:
            event: 触发本次记录的事件，用于定位会话。
        """
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain([Plain("⚠️ 本条对话记录保存失败，后续记忆可能不完整。")]),
        )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        await self._capture.capture_user(event)

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        await self._capture.capture_reply(event, resp)
