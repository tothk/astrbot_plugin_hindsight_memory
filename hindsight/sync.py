"""把本地未提交的原始对话装配成 Hindsight 的 retain 请求并投递。

本模块分两层：

- 装配层（``render_line`` / ``split_batches`` / ``build_items``）是纯函数，
  既不碰网络也不碰数据库，输入 ``RawMessage`` 列表、输出请求体，便于直接单测。
- 投递层（``HindsightSync``）负责「取未提交 → 装配 → POST → 标记已提交」的编排，
  数据库与 HTTP 客户端都由调用方注入。

本模块不 import astrbot：待提交记录的来源通过 ``UnflushedStore`` 协议注入，
这样在没有宿主的环境里也能测试完整的投递流程。
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol, Sequence

from ..db.models.models import RawMessage
from .client import HindsightClient, HindsightError

# 一条记录渲染成对话文本里的一行，行与行之间用换行拼接。
#
# 为什么要拼成这种「一行一条发言」的纯文本，而不是自己拼 JSON：Hindsight 官方
# 推荐的对话格式就是 ``Name (timestamp): text``，实测能把说话人解析成 entity、
# 把括号里的时间解析成 occurred_start。反过来，若用逗号/竖线/JSON 自己拼，正文里
# 本来就含这些字符，转义一漏整篇就坏了。
_LINE_SEPARATOR = "\n"


def speaker_of(raw: RawMessage) -> str:
    """取这条记录在对话文本里的说话人标识。

    优先用昵称而不是 ``sender_id``：Hindsight 会把 ``Name (time): text`` 里的
    Name 解析成实体，数字 ID（QQ 号）解析出来只是 "user"，此后「她喜欢看海」
    这类人格化事实就挂不到具体的人身上，召回时也匹配不到。实测同一段中文对话，
    用 "Alice" 能提取出 ``Involving: Alice``，用 "10001" 只能得到 ``Involving: user``。

    Args:
        raw: 待渲染的记录。

    Returns:
        昵称；昵称为空时退回 ``sender_id``（至少不会丢掉"有个人说过话"）。
    """
    name = (raw.sender_name or "").strip()
    return name or raw.sender_id


def render_line(raw: RawMessage) -> str:
    """把一条记录渲染成对话文本里的一行。

    Args:
        raw: 待提交的原始记录。

    Returns:
        形如 ``"小明 (2026-11-10T10:30:05+08:00): 今天有点累"`` 的单行文本。
        正文为空时只保留 ``"发送者 (时间):"`` 前缀。

    Note:
        正文里的换行会被压成空格。下游是按行识别说话人的，一条记录若自身跨行，
        第二行起就会变成没有说话人的残句，说话人归属整个错位——这比丢几个空格
        严重得多。
    """
    # splitlines() 而不是 replace("\n", " ")：Windows 的 \r\n 与旧 Mac 的 \r
    # 都能一并处理，顺带丢掉空行与每行首尾的空白。
    text = " ".join(part.strip() for part in raw.raw_message.splitlines() if part.strip())
    prefix = f"{speaker_of(raw)} ({raw.message_sent_time}):"
    return f"{prefix} {text}" if text else prefix


def split_batches(
    messages: Sequence[RawMessage], max_chars: int
) -> list[list[RawMessage]]:
    """按字符上限把记录切成若干批。

    规则：逐条累加，若「加入这一条」会超过上限，则把它之前的记录封成一批，
    它自己成为下一批的开头。

    Args:
        messages: 按 id 升序排列的待提交记录。
        max_chars: 单批的字符上限，通常是配置项 ``sync_batch_chars``。

    Returns:
        按原顺序切好的批次列表；输入为空时返回空列表。

    Raises:
        ValueError: ``max_chars`` 不是正数。

    Note:
        单条本身就超过上限时不会死循环：批为空时不封批，这条会被单独放进一批
        （随后下一条到来时触发封批）。否则「空批永远装不下」会让同步永久卡死。
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars 必须是正数，收到 {max_chars}")

    batches: list[list[RawMessage]] = []
    current: list[RawMessage] = []
    current_chars = 0

    for raw in messages:
        # +1 是这一行与上一行之间那个换行符的宽度。
        width = len(render_line(raw)) + 1
        if current and current_chars + width > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(raw)
        current_chars += width

    if current:
        batches.append(current)
    return batches


def build_items(batch: Sequence[RawMessage]) -> list[dict]:
    """把一批记录装配成 retain 接口的 ``items``。

    一批可能跨多个 ``document_id``（用户已确认允许）：批次只按字符数切分，
    不问文档归属。所以这里在提交前再按 ``document_id`` 归组，一个文档对应一个
    item —— 否则一批跨天时，后一天的内容会被塞进前一天的文档里，
    「document_id 按天区分记录」这个约定就破了。

    Args:
        batch: 同一批次内的记录，需保持时间顺序。

    Returns:
        ``MemoryItem`` 字典列表，每个元素对应一个文档。

    Note:
        - ``update_mode="append"``：同一个 ``document_id`` 之前提交过就追加而不是
          重建。这是用户定下的语义。
        - ``timestamp`` 取该文档内第一条记录的时间。不显式给的话，Hindsight 会把
          「摄取时刻」当成事件发生时间，事后问「她什么时候说累的」就会答成同步
          执行的那一刻。文档边界是一天，所以这里的粒度最多也就是一天。
    """
    lines_by_doc: dict[str, list[str]] = {}
    first_time_by_doc: dict[str, str] = {}

    for raw in batch:
        document_id = raw.document_id
        lines_by_doc.setdefault(document_id, []).append(render_line(raw))
        # setdefault 保证只记录第一次出现的时间，即该文档内最早的那条。
        first_time_by_doc.setdefault(document_id, raw.message_sent_time)

    return [
        {
            "content": _LINE_SEPARATOR.join(lines),
            "document_id": document_id,
            "update_mode": "append",
            "timestamp": first_time_by_doc[document_id],
        }
        # dict 保持插入顺序，所以 items 的顺序与记录首次出现的顺序一致。
        for document_id, lines in lines_by_doc.items()
    ]


class UnflushedStore(Protocol):
    """同步层需要的最小存储接口。

    由 ``SqlModelManager`` 实现；这里只声明形状，避免同步层直接依赖数据库实现，
    测试时可以用一个假对象替代。
    """

    def list_unflushed(self, limit: int) -> list[RawMessage]:
        """按 id 升序取出尚未提交的记录。"""
        ...

    def mark_flushed(self, ids: Sequence[int], value: int = 2) -> int:
        """把给定记录标记为指定状态，返回实际更新的行数。"""
        ...


@dataclass(frozen=True)
class SyncOutcome:
    """一次同步的结果。

    Attributes:
        messages: 本轮取出的待提交记录条数。
        batches: 切出的批次数。
        documents: 实际提交的 item 数（按文档计，通常大于批次数）。
        flushed: 成功标记为已提交的记录条数。
        error: 首个错误的描述；为 None 表示全部成功。
    """

    messages: int = 0
    batches: int = 0
    documents: int = 0
    flushed: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """本轮是否没有出错。"""
        return self.error is None


class HindsightSync:
    """把本地记录分批投递到 Hindsight。

    提交成功后才标记 ``flushed``，因此「POST 成功但标记前崩溃」会导致下次重复
    提交同一批内容——这是用户明确接受的代价（宁可重复，不可丢失）。
    """

    def __init__(
        self,
        store: UnflushedStore,
        client: HindsightClient,
        *,
        bank_id: str,
        max_chars: int,
        log,
    ) -> None:
        """初始化。

        Args:
            store: 待提交记录的来源，通常是 ``SqlModelManager``。
            client: 已配置好的 Hindsight HTTP 客户端。
            bank_id: 记忆库标识。
            max_chars: 单批字符上限，对应配置项 ``sync_batch_chars``。
            log: 日志对象，需提供 info/warning/error。
        """
        self._store = store
        self._client = client
        self._bank_id = bank_id
        self._max_chars = max_chars
        self._log = log

    async def run(self, *, limit: int = 1000) -> SyncOutcome:
        """执行一轮同步。

        Args:
            limit: 本轮最多取多少条，避免积压过多时一次性占满内存。

        Returns:
            本轮的结果摘要；出错时 ``error`` 非空，且未成功的批次不会被标记，
            下次会重新提交。

        Note:
        某一批失败后立即返回，不再尝试后续批次：典型失败原因（服务没起、网络
        不通、token 错）对后面每一批同样成立，继续重试只会把错误日志刷满。
        """
        # 退避与重试不在这里做：这一层只负责「尽可能提交一次」，调度节奏由上层决定。
        pending = await asyncio.to_thread(self._store.list_unflushed, limit)
        if not pending:
            self._log.debug("没有待提交的对话记录")
            return SyncOutcome()

        batches = split_batches(pending, self._max_chars)
        documents = 0
        flushed = 0

        for index, batch in enumerate(batches, start=1):
            items = build_items(batch)
            try:
                # 显式传 False：客户端默认已经改成异步提交，但这层还在用「200 =
                # 已落地」的老语义。改用异步 + 对账是下一步改造的事，在那之前
                # 必须钉住同步模式，否则 200 只代表排队，这里却会误标成已完成。
                result = await self._client.retain(
                    self._bank_id, items, is_async=False
                )
            except HindsightError as exc:
                awaited = f"第 {index}/{len(batches)} 批"
                self._log.error(f"Hindsight 提交失败（{awaited}）：{exc}")
                return SyncOutcome(
                    messages=len(pending),
                    batches=len(batches),
                    documents=documents,
                    flushed=flushed,
                    error=str(exc),
                )

            if not result.success:
                # 服务端回 200 但 success=false：把它当成失败处理，但不标记已提交，
                # 否则这批内容会永远留在本地之外。
                self._log.error(f"Hindsight 返回失败（第 {index}/{len(batches)} 批）")
                return SyncOutcome(
                    messages=len(pending),
                    batches=len(batches),
                    documents=documents,
                    flushed=flushed,
                    error="服务端返回 success=false",
                )

            ids = [raw.id for raw in batch if raw.id is not None]
            # 标记同样走线程：这是同步的 sqlite 更新，不能占用事件循环。
            await asyncio.to_thread(self._store.mark_flushed, ids)
            documents += len(items)
            flushed += len(ids)
            self._log.info(
                f"已提交第 {index}/{len(batches)} 批："
                f"{len(ids)} 条记录 / {len(items)} 个文档 → bank={self._bank_id}"
            )

        return SyncOutcome(
            messages=len(pending),
            batches=len(batches),
            documents=documents,
            flushed=flushed,
        )
