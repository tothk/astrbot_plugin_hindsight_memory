from datetime import datetime
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
from .core.capture import MessageCapture, bot_display_name
from .core.memory_service import MemoryService, format_sync_result
from .core.persona import persona_from_conversation
from .core.recall import prune_previous_memories
from .core.recorder import Recorder, build_raw_message, resolve_bot_id
from .hindsight.plugin_config import HindsightConfig


# 工具是**全局挂载**的：宿主在 tools 为 None 时直接遍历
# ``get_full_tool_set()`` 把所有 active 工具挂上（见 astr_agent_tool_exec.py），
# 工具注册也只绑 ``EventType.OnCallingFuncToolEvent``、不带任何 event_filter。
# 因此「只服务私聊」这条约束没法用装饰器表达，只能建在工具函数内部——
# 而且必须在任何召回/写入之前，否则成了「先读了记忆再拒绝」。
# 这两段文本是给**模型**看的：它得知道自己做不到，才不会转头对用户说
# 「我已经记住了」。
_RECALL_NOT_PRIVATE = (
    "本插件只在一对一私聊中提供记忆能力，群聊里不会把任何人的记忆读出来。"
)
_RETAIN_NOT_PRIVATE = (
    "本插件只在一对一私聊中记录记忆，群聊里的发言不会被写入任何人的记忆库。"
)


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
        # 检索词取事件里的用户原话，不取 req.prompt。req.prompt 的源头虽然
        # 也是 event.message_str（astr_main_agent/request.py:273），但钩子跑
        # 之前它已经被改写过：
        # 1. 配置了 prompt_prefix 会被拼上人设前缀（request.py:50-63 的
        #    apply_prompt_prefix，prepare_request 在 :608 调），检索词变成
        #    「前缀 + 原话」，前缀会稀释 embedding。
        # 2. 带附件时没有正文，它会被填成占位符——"<attachment>"
        #    （request.py:578）、"[Image unavailable]"
        #    （utils/image_input.py:135）、"总结一下文件里面讲了什么？"
        #    （request.py:152）。拿这些去检索只会白跑一次请求。
        # 3. 其它插件也可能在 OnLLMRequestEvent 里改写它。
        # 所以不做 req.prompt 兜底：message_str 为空就说明这条消息本来没有
        # 可检索的文本。message_str 在 waking_check/stage.py:132-136 已剥掉
        # 唤醒前缀，AgentWakeupEvent 也填了它（agent_wakeup_event.py:70），
        # 不需要兜底。
        text = event.message_str
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

    @filter.llm_tool()
    async def proactive_recall(self, event: AstrMessageEvent, query: str) -> str:
        """主动检索与该话题相关的既往记忆。

        每轮对话前都会自动召回一次；只有当用户提到更久远、更具体的事情
        （例如「我们之前说的那件事」），而当前上下文里没有相关记忆时，
        才用这个工具补充检索一次。

        Args:
            query(string): 想要召回的记忆描述，用自然语言写清楚要找什么
        """
        # 闸门必须在召回之前：群聊里跑一次召回，等于把这一对一记忆库的内容
        # 读进了一个多人可见的上下文。
        if not event.is_private_chat():
            return _RECALL_NOT_PRIVATE

        query = (query or "").strip()
        if not query:
            return "没有提供检索内容，未召回任何记忆。"

        # 复用自动召回的整条链路：它已经带了预算、类型过滤、超时与库不存在的
        # 兜底，返回的文本也已经是可读的「【相关记忆】…」格式。
        memory = await self._memory.recall_for(query)
        if not memory:
            return "没有找到相关的记忆。"

        # 返回值（str）会被追加到下一次 LLM 请求里供模型参考；
        # 这里不能返回 MessageEventResult——那会被当成「工具已直接回复用户」，
        # 把记忆原文当作正式回复发出去，并且终止本轮 agent 循环。
        return memory

    @filter.llm_tool()
    async def proactive_retain(
        self, event: AstrMessageEvent, query: list[str]
    ) -> str:
        """把你自己说的话或判断写入长期记忆。

        这个工具记的是**你的第一人称内容**——你的承诺、结论、决定，不是用户
        刚说的话（那些会自动记录）。只有当你判断某段内容值得立刻记住、或发现
        它没被自动记录下来时，才调用这个工具补写。

        Args:
            query(list[str]): 你要记住的内容，每条字符串是一行，行内用你自己的口吻
        """
        # 同上：写之前就挡住。群聊里的发言属于多人，落进一对一记忆库是
        # 拿别人的话冒充这个人的过往。
        if not event.is_private_chat():
            return _RETAIN_NOT_PRIVATE

        lines = [str(item).strip() for item in (query or []) if str(item).strip()]
        if not lines:
            return "没有提供要写入的内容，未写入任何记忆。"

        # 整批一起写：一是省掉逐条 await 与各自的数据库事务，二是要么整批
        # 进库、要么整批失败，不会留下「前两行进去了、后三行没有」的半批，
        # 返回给模型的文案才能说准。
        persona_id = await persona_from_conversation(self.context, event)
        # 走到这里的内容是模型自己的口吻（「我答应明天提醒你」这类），说的是它
        # 自己，不是用户。role 必须记成 llm：
        # 1. speaker_of() 按 role 选称呼，记成 user 会让**用户昵称**盖到 AI 的话
        #    上，抽取出来就变成「小满答应明天提醒吃药」——AI 的承诺成了用户的
        #    行为，之后召回时人格全错位。
        # 2. content_hash 含 role，同一句话被双方各说一遍时不会撞唯一索引。
        # 昵称这里存的是**来源值**（人格名），配置里的 llm_nickname 交给渲染层
        # 覆盖（见 sync.speaker_of）：在记录阶段就把配置烤进库，第二层「库里
        # 的昵称」就永远用不上，改配置也改不动历史记录。
        bot_id = resolve_bot_id(event.message_obj.self_id, event.get_platform_id())
        bot_name = bot_display_name(self.context, persona_id)
        # 同一个 now：这批内容是同一次调用的产物，时间戳与 document_id 都该一致。
        now = datetime.now()
        rows = [
            build_raw_message(
                role="llm",
                sender_id=bot_id,
                sender_name=bot_name,
                text=line,
                umo=event.unified_msg_origin,
                persona_id=persona_id or "",
                now=now,
            )
            for line in lines
        ]

        result = await self._recorder.save_many(
            rows,
            lambda: self._notify_save_failed(event),
            # 不触发「攒够字数投一轮积压」：本函数紧接着只投这几行。
            # 若这里也回调累加器，一旦把 pending 顶过 batch_chars，后台那轮
            # 会把刚写的行扫走投一次，随后 sync_records 又投一次——同一段
            # 内容在服务端文档里追加两遍，且本地多一条 SyncOperation。
            trigger_backlog=False,
        )
        if not result.ok:
            return "写入失败，本轮对话的记忆没有保存下来。"
        if not result.rows:
            # 全部撞上唯一索引：内容此前已经记过，不是故障。
            return "这些内容和已有记录重复，没有新的记忆需要写入。"

        saved = len(result.rows)

        # 只投递刚写进去的这几行，不是库里所有积压，也不触发对账——
        # 模型说「我记住了」，就应该只对这几句负责。
        try:
            outcome = await self._memory.sync_records(result.rows, trigger="主动写入")
        except Exception as e:
            self.logger.error(f"主动写入后投递异常: {e}")
            return (
                f"已记录 {saved} 条对话到本地，但投递到 Hindsight 时出错：{e}。"
                "记录不会丢失，稍后会自动重试投递。"
            )

        if not outcome.ok:
            return (
                f"已记录 {saved} 条对话到本地，但投递到 Hindsight 失败：{outcome.error}。"
                "记录不会丢失，稍后会自动重试投递。"
            )
        if not outcome.flushed:
            return f"已记录 {saved} 条对话，投递已排队（服务端仍在处理中）。"
        if result.duplicates:
            return (
                f"已写入 {outcome.flushed} 条对话，"
                f"另外 {result.duplicates} 条与已有记录重复，未重复写入。"
            )
        return f"已将 {outcome.flushed} 条对话写入长期记忆。"

