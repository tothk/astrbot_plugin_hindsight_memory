from pathlib import Path
from datetime import datetime, date
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform import MessageType
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .config.config_manager import ConfigManager
from .config.plugin_paths import PluginPaths
from .db.models.sql_model_manager import SqlModelManager
from .db.models.models import RawMessage
from astrbot.api import logger
from .core.persona import persona_for_reply, persona_from_conversation


# 插件元数据由 metadata.yaml 提供。AstrBot 3.5.19+ 会自动识别继承 Star 的插件类，
# 已废弃的 @register 装饰器不再使用，避免宿主未来移除它时插件无法加载。
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        self.PLUGIN_DATA_PATH = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self._plugin_paths = PluginPaths(self.PLUGIN_DATA_PATH)
        self._config_manager = ConfigManager(self._plugin_paths)
        self._sql = SqlModelManager(self._plugin_paths)
        self._db_warned = False

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

        # 插件卸载/停用时的清理。
        self._sql.close()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        # message_str = event.message_str  # 获取消息的纯文本内容
        message_str = event.get_message_outline()  # 获取消息的纯文本内容
        # 屏蔽指令
        if message_str.startswith("/") or message_str.startswith("／"):
            return
        # 屏蔽非文本对话导致的空值
        # if message_str != "":
        if message_str != "":
            persona_id = await persona_from_conversation(self.context, event)
            try:
                self._sql.insert_raw(
                    RawMessage(
                        record_date=date.today().isoformat(),
                        document_id=f"{date.today().isoformat()}:{event.unified_msg_origin}",
                        sender_id=event.get_sender_id(),
                        persona_id=persona_id or "",
                        role="user",
                        raw_message=message_str,
                        message_sent_time=datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                )
                self._db_warned = False
            except Exception as e:
                logger.error(f"写入原始记录失败: {e}")
                if not self._db_warned:  # ← 只有第一次失败才提示
                    self._db_warned = True
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain([Plain("⚠️ 本条对话记录保存失败，后续记忆可能不完整。")]),
                    )

    @filter.on_llm_response()
    async def on_llm_resp(
            self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """ 抓取对话记录写入sqlite数据库。 """
        message_type = event.get_message_type()
        if message_type != MessageType.FRIEND_MESSAGE: return
        # 判据用停止信号
        if (
                resp.role == "err"
                or event.is_stopped()
                or event.get_extra("agent_stop_requested")
        ):
            return
        raw_message = resp.completion_text
        if not raw_message:
            return
        bot_id = event.message_obj.self_id
        if not bot_id or bot_id == "unknown_selfid":
            bot_id = event.get_platform_id()
        persona_id = await persona_for_reply(self.context, event)
        try:
            self._sql.insert_raw(
                RawMessage(
                    record_date=date.today().isoformat(),
                    document_id=f"{date.today().isoformat()}:{event.unified_msg_origin}",
                    sender_id=bot_id,
                    persona_id=persona_id or "",
                    role="llm",
                    raw_message=raw_message,
                    message_sent_time=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
            )
            self._db_warned = False
        except Exception as e:
            logger.error(f"写入原始记录失败: {e}")
            if not self._db_warned:  # ← 只有第一次失败才提示
                self._db_warned = True
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain("⚠️ 本条对话记录保存失败，后续记忆可能不完整。")]),
                )
