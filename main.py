from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.platform import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .config.plugin_paths import PluginPaths
from .db.models.sql_model_manager import SqlModelManager
from .core.capture import MessageCapture
from .core.memory_service import MemoryService, format_sync_result
from .core.recall import prune_previous_memories
from .core.recorder import Recorder
from .hindsight.plugin_config import HindsightConfig


# 插件元数据由 metadata.yaml 提供。AstrBot 3.5.19+ 会自动识别继承 Star 的插件类，
# 已废弃的 @register 装饰器不再使用，避免宿主未来移除它时插件无法加载。
class HindsightMemory(Star):
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
        # 解析成强类型对象：键名只在这一处出现，避免散落各处改漏。
        self._hindsight = HindsightConfig.from_mapping(self._config)
        self.PLUGIN_DATA_PATH = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self._plugin_paths = PluginPaths(self.PLUGIN_DATA_PATH)
        # self.logger 是 AstrBot 为本插件单独准备的 logger（面板里可单独调级别），
        # 比模块级的 astrbot 全局 logger 更适合记录运行期信息。
        self._sql = SqlModelManager(self._plugin_paths, self.logger)
        # 先建记忆服务，再建 Recorder：Recorder 需要把「又存了一条」的事件
        # 报给它，攒够字数才能提前投递。两者之间只有这一条单向依赖。
        self._memory = MemoryService(self._hindsight, self._sql, self.logger)
        self._recorder = Recorder(self._sql, self.logger, self._memory.note_saved)
        self._capture = MessageCapture(
            self.context, self._recorder, self._notify_save_failed, self.logger
        )

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 用插件专属 logger：面板里可以单独调它的级别，不会淹没在全局日志里。
        self.logger.info("Hindsight 记忆储存插件已加载，开始记录原始对话。")
        # 定时补投要挂在运行中的事件循环上，只能在 initialize 里启动。
        await self._memory.start()

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

        # 插件卸载/停用时的清理。
        await self._memory.stop()
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

    @filter.command("hindsight_sync")
    async def hindsight_sync(self, event: AstrMessageEvent):
        """手动把本地待同步的对话记录投递到 Hindsight。

        定时补投要等到次日 sync_daily_time 才跑第一次，装机当天看不到任何效果，
        排查时也没有手段立刻验证链路是否通——这条指令就是那个开关。

        Args:
            event: 触发本条指令的消息事件。

        Yields:
            一条纯文本结果，告知投递了多少条或失败原因。
        """
        # 指令里不抛异常：handler 抛出去会被 AstrBot 兜住并回一句「插件出现异常」，
        # 用户看到的是框架话术而不是「哪一步没成功」。sync_once 内部已经兜了两层，
        # 这里再包一次是为了连它自己出意外（比如配置对象是坏的）也能给出可读结果。
        try:
            outcome = await self._memory.sync_once(trigger="手动指令")
        except Exception as e:
            self.logger.error(f"手动同步指令异常: {e}")
            yield event.plain_result(f"❌ 同步失败：{e}")
            return
        yield event.plain_result(format_sync_result(outcome))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """把召回的记忆挂到本轮用户消息上。

        走 on_llm_request 而不是 on_llm_response：记忆必须在请求发出**之前**
        进入上下文，否则这一轮的回答用不上它。

        Args:
            event: 当前消息事件。
            req: 即将发给模型的请求，``extra_user_content_parts`` 可在此追加。
        """
        # 群聊一律不注入：本插件只服务一对一陪伴场景，且群聊里混入他人记忆
        # 是隐私事故。
        if event.get_message_type() != MessageType.FRIEND_MESSAGE:
            return
        text = req.prompt or event.message_str
        if not text:
            return
        memory = await self._memory.recall_for(text)
        if not memory:
            return
        # 挂在用户消息里而不是改 system_prompt：
        # 1. 缓存。system_prompt 是发给模型的第 0 条消息（见
        #    tool_loop_agent_runner 里 `messages.insert(0, ...)`），而召回内容
        #    每轮都不同——一改第 0 条，前面所有历史的前缀缓存全部作废。
        #    挂在最后一条用户消息上，历史部分仍能命中缓存。
        # 2. 语义。记忆是「这次对话需要的背景」，不是「你是谁」的设定，
        #    放用户消息里更贴合它在上下文中的角色。
        # 先摘掉自己上一轮留下的：同一个请求被处理两遍时（热重载导致 handler
        # 重复注册等），裸 append 会让用户消息里叠出两段记忆。只认自己的页眉，
        # 不动知识库等插件写的片段。
        prune_previous_memories(req.extra_user_content_parts)
        # mark_as_temp()：只在本次请求里带给模型，不写进会话历史。不加它，
        # 每轮召回的片段会被 _save_to_history 累积进 history，越滚越大。
        req.extra_user_content_parts.append(TextPart(text=memory).mark_as_temp())

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        await self._capture.capture_reply(event, resp)
