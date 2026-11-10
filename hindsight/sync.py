"""把本地未提交的原始对话装配成 Hindsight 的 retain 请求并投递，随后对账。

本模块分三层：

- 装配层（``render_line`` / ``split_batches`` / ``build_items``）是纯函数，
  既不碰网络也不碰数据库，输入 ``RawMessage`` 列表、输出请求体，便于直接单测。
- 投递层（``HindsightSync``）负责「取未提交 → 装配 → 排队 → 记下操作凭据」。
- 对账层（同属 ``HindsightSync``）负责回查上一轮排队的操作，确认成功或重试。

为什么要拆成「排队」和「对账」两段：实测一次同步 retain 要 80 秒以上（3 行
中文对话，从提交到父操作 completed 耗时 86 秒），HTTP 客户端等不起，插件更
不能把事件循环挂在那里。所以提交只负责排队，真正的结果靠 operation 状态回查。

本模块不 import astrbot：存储与 HTTP 客户端都由调用方注入，这样在没有宿主的
环境里也能测试完整的投递与对账流程。
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from ..db.models.models import RawMessage, SyncOperation
from .client import HindsightClient, HindsightError, HindsightOperationNotFoundError

# 一条记录渲染成对话文本里的一行，行与行之间用换行拼接。
#
# 为什么要拼成这种「一行一条发言」的纯文本，而不是自己拼 JSON：Hindsight 官方
# 推荐的对话格式就是 ``Name (timestamp): text``，实测能把说话人解析成 entity、
# 把括号里的时间解析成 occurred_start。反过来，若用逗号/竖线/JSON 自己拼，正文里
# 本来就含这些字符，转义一漏整篇就坏了。
_LINE_SEPARATOR = "\n"

# 同一个批次最多重试几次后放弃。
#
# 超过这个次数说明内容本身有问题（比如模型解析不了），继续 retry 只是反复消耗
# 服务端的 LLM 额度。放弃时把记录退回待投递：宁可文档末尾重复一遍，也不能让
# 用户的记录永远卡在「已入队」这个中间态里。
MAX_ATTEMPTS = 3


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


class SyncStore(Protocol):
    """同步层需要的最小存储接口。

    由 ``SqlModelManager`` 实现；这里只声明形状，避免同步层直接依赖数据库实现，
    测试时可以用一个假对象替代。
    """

    def list_unflushed(self, limit: int) -> list[RawMessage]:
        """按 id 升序取出还没入队的记录。"""
        ...

    def mark_flushed(self, ids: Sequence[int], value: int = 2) -> int:
        """把给定记录标记为指定状态，返回实际更新的行数。"""
        ...

    def mark_enqueued(
        self,
        ids: Sequence[int],
        *,
        operation_id: str,
        items_count: int = 0,
    ) -> int:
        """把一批记录标为「已入队」并写下操作凭据。"""
        ...

    def pending_operations(self, limit: int = 100) -> list[SyncOperation]:
        """取出还没有结论的批次。"""
        ...

    def resolve_operation(
        self,
        operation_id: str,
        *,
        status: str,
        error: str = "",
        attempts: int | None = None,
    ) -> int:
        """记录某个批次的结论。"""
        ...

    def release_operation(self, operation_id: str) -> int:
        """撤回一个批次，把记录退回待投递。"""
        ...


@dataclass(frozen=True)
class SyncOutcome:
    """一次同步的结果。

    Attributes:
        messages: 本轮取出的待入队记录条数。
        batches: 切出的批次数。
        documents: 实际提交的 item 数（按文档计，通常大于批次数）。
        flushed: 成功标为「已入队」的记录条数。
        completed: 对账确认服务端已完成的批次数。
        retried: 对账后重新排队的批次数。
        released: 对账后放弃、记录退回待投递的批次数。
        error: 首个错误的描述；为 None 表示本轮没有出错。

    Note:
        ``error`` 只描述「投递新内容」阶段的失败。对账失败不写进这里：某一批
        对不上账不代表这一轮没有把新记录送出去，把它混进同一个字段会让上层
        误以为整个同步都失败了。
    """

    messages: int = 0
    batches: int = 0
    documents: int = 0
    flushed: int = 0
    completed: int = 0
    retried: int = 0
    released: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """本轮是否没有出错。"""
        return self.error is None


class HindsightSync:
    """把本地记录分批排队到 Hindsight，并回查上一轮的结果。

    一次 ``run()`` 做两件事，顺序不能反：

    1. 先对账：上一轮入队的操作可能有结论了。确认完成的把记录标成终态，
       失败的先重新排队、屡次失败才退回待投递。
    2. 再投递：把还没入队的记录装配成新批次排队，并记下操作凭据。

    先对账的意义不只是「顺便查一下」：投递会把记录标成「已入队」，若不对账，
    这些记录就永远停在中间态，谁也不知道它们到底进没进服务端。
    """

    def __init__(
        self,
        store: SyncStore,
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
            log: 日志对象，需提供 info/warning/error/debug。
        """
        self._store = store
        self._client = client
        self._bank_id = bank_id
        self._max_chars = max_chars
        self._log = log

    async def run(self, *, limit: int = 1000) -> SyncOutcome:
        """执行一轮同步：先对账，再投递。

        Args:
            limit: 本轮最多取多少条待入队记录，避免积压过多时一次性占满内存。

        Returns:
            本轮的结果摘要。对账的计数在 ``completed`` / ``retried`` /
            ``released`` 里，投递的计数在 ``flushed`` 里；``error`` 只反映投递
            阶段的失败。

        Note:
            投递时某一批失败后立即返回，不再尝试后续批次：典型失败原因（服务
            没起、网络不通、token 错）对后面每一批同样成立，继续重试只会把错误
            日志刷满。
        """
        outcome = await self.reconcile()
        submitted = await self.submit(limit=limit)

        return SyncOutcome(
            messages=submitted.messages,
            batches=submitted.batches,
            documents=submitted.documents,
            flushed=submitted.flushed,
            completed=outcome.completed,
            retried=outcome.retried,
            released=outcome.released,
            error=submitted.error,
        )

    async def reconcile(self) -> SyncOutcome:
        """回查所有还没结论的批次。

        Returns:
            只填 ``completed`` / ``retried`` / ``released`` 三个计数的摘要。

        Note:
            单个批次查不到就跳过，不影响别的批次：服务端可能临时抽风，下一轮
            再查就是了。
        """
        completed = retried = released = 0

        try:
            pending = await asyncio.to_thread(self._store.pending_operations)
        except Exception as exc:
            # 对账读不到批次不该让整轮同步失败：投递新内容仍然有意义。
            self._log.error(f"读取待对账批次失败: {exc}")
            return SyncOutcome()

        for record in pending:
            operation_id = record.operation_id
            try:
                data = await self._client.get_operation(self._bank_id, operation_id)
            except HindsightOperationNotFoundError:
                # 服务端已经没有这个操作了（被清理或过期），retry 也无从谈起。
                released += self._release(operation_id, "服务端已无此操作")
                continue
            except HindsightError as exc:
                self._log.warning(f"查询操作状态失败（{operation_id}）：{exc}")
                continue

            status = str(data.get("status") or "").lower()

            if status == "completed":
                resolved = await asyncio.to_thread(
                    self._store.resolve_operation, operation_id, status="completed"
                )
                completed += 1
                self._log.info(
                    f"对账完成：operation={operation_id} {resolved} 条记录已确认落地"
                )
                continue

            if status in ("failed", "cancelled", "not_found"):
                retried, released = await self._handle_failure(
                    record, status=str(data.get("error_message") or status),
                    retried=retried, released=released,
                )
                continue

            # pending / processing：还在跑，下一轮再看。
            self._log.debug(f"操作仍在处理中（{operation_id}）：{status or '未知'}")

        return SyncOutcome(completed=completed, retried=retried, released=released)

    async def _handle_failure(
        self,
        record: SyncOperation,
        *,
        status: str,
        retried: int,
        released: int,
    ) -> tuple[int, int]:
        """处理一个失败的批次：先重试，屡败则撤回。

        Args:
            record: 本地记下的批次凭据。
            status: 失败原因描述。
            retried: 已经重试过的批次数，用于累加。
            released: 已经撤回的批次数，用于累加。

        Returns:
            更新后的 ``(retried, released)``。

        Note:
            为什么先 retry 而不是直接重投：``update_mode="append"`` 下重投会把
            同一段内容在文档末尾再追加一遍，服务端的 retry 则是就地重做原操作，
            不会产生重复。只有 retry 也救不回来时才退回重投。
        """
        operation_id = record.operation_id
        attempts = (record.attempts or 0) + 1

        if attempts >= MAX_ATTEMPTS:
            released += self._release(
                operation_id, f"已失败 {attempts} 次：{status}"
            )
            return retried, released

        try:
            await self._client.retry_operation(self._bank_id, operation_id)
        except HindsightOperationNotFoundError:
            released += self._release(operation_id, "重试时发现操作已不存在")
            return retried, released
        except HindsightError as exc:
            # 连 retry 请求都发不出去（服务挂了），保持 failed，下一轮再试。
            self._log.warning(f"重新排队失败（{operation_id}）：{exc}")
            await asyncio.to_thread(
                self._store.resolve_operation,
                operation_id,
                status="failed",
                error=f"{status} / 重试失败：{exc}",
                attempts=attempts,
            )
            return retried, released

        await asyncio.to_thread(
            self._store.resolve_operation,
            operation_id,
            status="pending",
            error=status,
            attempts=attempts,
        )
        self._log.warning(
            f"对账发现失败，已重新排队（{operation_id}，第 {attempts} 次）：{status}"
        )
        return retried + 1, released

    def _release(self, operation_id: str, reason: str) -> int:
        """放弃一个批次，把记录退回待投递。

        Args:
            operation_id: 批次的父操作标识。
            reason: 放弃的原因，写进日志便于排查。

        Returns:
            被退回待投递的行数。
        """
        released_rows = self._store.release_operation(operation_id)
        self._log.warning(
            f"放弃批次 operation={operation_id}（{reason}），"
            f"{released_rows} 条记录退回待投递"
        )
        return 1

    async def submit(self, *, limit: int = 1000) -> SyncOutcome:
        """把还没入队的记录装配成批次，排队后记下操作凭据。

        Args:
            limit: 本轮最多取多少条。

        Returns:
            投递阶段的摘要：``messages`` / ``batches`` / ``documents`` /
            ``flushed``（成功入队的条数），失败时 ``error`` 非空。
        """
        pending = await asyncio.to_thread(self._store.list_unflushed, limit)
        if not pending:
            self._log.debug("没有待提交的对话记录")
            return SyncOutcome()

        batches = split_batches(pending, self._max_chars)
        documents = 0
        flushed = 0

        for index, batch in enumerate(batches, start=1):
            items = build_items(batch)
            ids = [raw.id for raw in batch if raw.id is not None]
            awaited = f"第 {index}/{len(batches)} 批"

            # 客户端自带 operation_id：它是服务端的幂等键，同 id 重发只会返回
            # 原操作而不会产生第二份内容。所以这里先在本地生成，超时后用同 id
            # 重发依然安全。
            operation_id = str(uuid.uuid4())
            try:
                result = await self._client.retain(
                    self._bank_id,
                    items,
                    is_async=True,
                    operation_id=operation_id,
                )
            except HindsightError as exc:
                self._log.error(f"Hindsight 提交失败（{awaited}）：{exc}")
                return SyncOutcome(
                    messages=len(pending),
                    batches=len(batches),
                    documents=documents,
                    flushed=flushed,
                    error=str(exc),
                )

            if not result.success:
                # 服务端回 200 但 success=false：把它当成失败处理，但不标记已入队，
                # 否则这批内容会永远留在本地之外。
                self._log.error(f"Hindsight 返回失败（{awaited}）")
                return SyncOutcome(
                    messages=len(pending),
                    batches=len(batches),
                    documents=documents,
                    flushed=flushed,
                    error="服务端返回 success=false",
                )

            if not result.operation_id:
                # 理论上异步提交一定会回 operation_id。真没有的话这批次无从对账，
                # 只能按「服务端已收下」处理：留在待投递会被重复追加，比误判完成更糟。
                self._log.warning(
                    f"{awaited} 未返回 operation_id，无法对账，按已提交处理"
                )
                await asyncio.to_thread(
                    self._store.mark_flushed, ids, 2
                )
                documents += len(items)
                flushed += len(ids)
                continue

            await asyncio.to_thread(
                self._store.mark_enqueued,
                ids,
                operation_id=result.operation_id,
                items_count=len(items),
            )
            documents += len(items)
            flushed += len(ids)
            self._log.info(
                f"已排队 {awaited}：{len(ids)} 条记录 / {len(items)} 个文档 → "
                f"bank={self._bank_id} operation={result.operation_id}"
            )

        return SyncOutcome(
            messages=len(pending),
            batches=len(batches),
            documents=documents,
            flushed=flushed,
        )
