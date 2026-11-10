"""Hindsight HTTP API 客户端。

本模块只做一件事：把请求发出去，把响应解回来。分批、调度、重试策略都不在这里。

刻意不 import astrbot：网络层与宿主解耦，既方便单元测试，也方便将来脱离
AstrBot 复用。日志、重试、告警全部由调用方负责。

接口依据 Hindsight HTTP API 1.2.0（见 docs/hindsight-api.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

# 写入/召回默认超时。
#
# 同一台机器上实测：一次同步 retain（3 行中文对话）从提交到抽取完成要 86 秒，
# 远远超过 HTTP 客户端的合理等待时间；而召回必须在几秒内返回，慢了就会拖住
# 用户这一轮对话。所以两组超时必须分开：
#   - 写入走异步模式（async=True），HTTP 只负责排队，很快返回；
#   - 同步写入只用于手工诊断，给一个宽松值；
#   - 召回用短超时，宁可这轮不带记忆，也不要让用户干等。
DEFAULT_TIMEOUT = 30.0
RETAIN_TIMEOUT = 120.0
RECALL_TIMEOUT = 10.0


class HindsightError(Exception):
    """所有 Hindsight 客户端异常的基类。"""


class HindsightConnectionError(HindsightError):
    """网络层失败：连不上、超时、DNS 解析失败等。

    这类错误几乎总是可重试的（服务端暂时不可达）。
    """


class HindsightAuthError(HindsightError):
    """鉴权失败（HTTP 401 / 403）。

    调用方需要分两种情况给用户提示：
        - `token_present` 为 False：服务端要求鉴权，但插件没配 Token；
        - `token_present` 为 True：Token 无效或已过期。
    """

    def __init__(self, message: str, *, token_present: bool) -> None:
        super().__init__(message)
        self.token_present = token_present


class HindsightRequestError(HindsightError):
    """请求本身不被接受（HTTP 4xx，鉴权错误除外）。

    422 表示请求体校验失败，通常是字段类型或必填项问题。
    这类错误重试没有意义，需要改代码或改配置。
    """


class HindsightBankNotFoundError(HindsightRequestError):
    """目标记忆库不存在（HTTP 404）。

    单独成一类是因为它可恢复：服务端不会因为你 retain 就凭空建库，而召回
    一个还没写过东西的库会稳定返回 404。调用方可以 ``ensure_bank()`` 建库
    再重试，其它 4xx 则没有这种补救。

    Note:
        客户端里所有业务路径都挂在 bank 下，因此这里把 404 一律当作
        「库不存在」。需要区分「操作不存在」时用
        ``HindsightOperationNotFoundError``。
    """


class HindsightOperationNotFoundError(HindsightRequestError):
    """要查询/重试的操作不存在（HTTP 404）。

    与 ``HindsightBankNotFoundError`` 同为 404，但补救方式不同：库不存在可以
    建，操作不存在只说明服务端已经把它清理掉了，本地只能放弃这条路径、
    回退成重新投递原始内容。

    Note:
        服务端的 operations 接口对不存在的 id 也可能返回 200 且
        ``status="not_found"``，因此调用方除了捕获本异常，还要检查响应里的
        ``status``。
    """


class HindsightServerError(HindsightError):
    """服务端错误（HTTP 5xx），或响应体不是合法 JSON。"""


@dataclass(frozen=True)
class RetainResult:
    """一次 retain 的响应。

    Attributes:
        success: 服务端是否报告无错完成。
        bank_id: 接收内容的记忆库。
        items_count: 处理的 item 数量。
        is_async: 是否异步处理。True 意味着 HTTP 200 只代表请求被接受，
            事实尚未落地，必须再拿 ``operation_id`` 去查进度。
        operation_id: 异步操作的标识。仅 ``is_async`` 为 True 时存在。
            服务端会原样回显客户端提供的 id（实测确认），因此调用方可以
            用自己生成的 UUID 作为幂等键。
        usage: LLM token 用量，仅同步执行时才有；无则返回空字典。
    """

    success: bool
    bank_id: str
    items_count: int
    is_async: bool
    operation_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class HindsightClient:
    """Hindsight 记忆服务的异步 HTTP 客户端。

    用法::

        client = HindsightClient.from_config("localhost", 8888, "")
        try:
            result = await client.retain("my-bank", [{"content": "..."}])
        finally:
            await client.aclose()

    也可以自己管理连接池，把复用一个 ``httpx.AsyncClient`` 传进来。
    这在插件场景下是推荐做法：每个进程一个连接池，`terminate` 时关闭。

    Args:
        base_url: 服务地址，如 ``http://localhost:8888``。缺少 scheme 时
            自动补 ``http://``，末尾多余的斜杠会被去掉。
        token: 鉴权 Token。为空或 None 时**完全不发送** ``Authorization`` 头
            —— 不能发空值，很多服务端把「有头但值为空」判为非法而返回 401。
        timeout: 单次请求超时（秒）。
        http_client: 可选的现成 ``httpx.AsyncClient``。传入时由调用方负责
            关闭；不传则本对象自建并在 ``aclose()`` 时关闭。
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._token = (token or "").strip()
        self._timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_config(
        cls,
        host: str,
        port: int,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HindsightClient:
        """按插件配置的 host / port / token 构造客户端。

        Args:
            host: 主机名或完整 URL。允许已经带 ``http://`` 或端口。
            port: 端口号。``host`` 里已含端口时以 host 为准。
            token: 鉴权 Token，可为空。
            timeout: 单次请求超时（秒）。

        Returns:
            未建立连接的客户端实例（httpx 在首次请求时才真正建连）。
        """
        base = host.strip().rstrip("/")
        if "://" not in base:
            base = f"http://{base}"
        # host 里已经写了端口（形如 "host:8888"）就不要再拼一次。
        if _has_explicit_port(base):
            base_url = base
        else:
            base_url = f"{base}:{port}"
        return cls(base_url, token=token, timeout=timeout)

    @property
    def base_url(self) -> str:
        """规范化后的服务地址，便于日志与排错。"""
        return self._base_url

    @property
    def token_present(self) -> bool:
        """是否配置了鉴权 Token。用于把 401/403 分流成不同提示。"""
        return bool(self._token)

    # ------------------------------------------------------------------
    # 业务方法
    # ------------------------------------------------------------------

    async def retain(
        self,
        bank_id: str,
        items: list[dict],
        *,
        is_async: bool = True,
        operation_id: str | None = None,
        timeout: float | None = None,
    ) -> RetainResult:
        """写入一批内容。

        Args:
            bank_id: 记忆库标识。
            items: ``MemoryItem`` 字典列表，每项至少要有 ``content``。
                若要追加到已有文档，需同时给 ``document_id`` 与
                ``update_mode="append"``（Hindsight 的硬性要求）。
            is_async: 是否异步提交。默认 True：HTTP 只负责排队，抽取在服务端
                后台进行。同步模式实测要 80 秒以上，HTTP 客户端等不起。
            operation_id: 异步操作的标识，由调用方生成。服务端会原样回显，
                并且它是幂等键——用同一个 id 重发不会产生第二份内容。
                同步模式下会被服务端忽略。

                注意：这个保证属于「持久化 id + 提交」这个组合，不属于
                ``retain()`` 本身。调用方必须在**发出请求之前**把 id 存下来，
                否则进程一崩就不知道下一轮该用哪个 id 重发，重发出来的是
                第二份内容。本项目的做法见 ``HindsightSync.submit()``：
                先 ``mark_enqueued`` 落库，再调这里。
            timeout: 覆盖本次请求的超时（秒）。异步提交通常几秒内返回，
                同步提交需要传一个宽松值。

        Returns:
            解析后的 ``RetainResult``。

        Raises:
            HindsightConnectionError: 网络不可达或超时（可重试）。
            HindsightAuthError: 鉴权失败。
            HindsightRequestError: 请求体被拒绝，如 ``items`` 为空导致的 422。
            HindsightServerError: 服务端 5xx 或响应不是合法 JSON。
        """
        if not items:
            # 空数组会被服务端当校验错误（422）。在本地就拦住，
            # 避免把"没有新内容可提交"误报成故障。
            raise ValueError("items 不能为空：没有待提交的内容时不要调用 retain")

        payload = _drop_none(
            {
                "items": items,
                "async": is_async,
                "operation_id": operation_id,
            }
        )
        path = f"/v1/default/banks/{_quote_bank(bank_id)}/memories"
        data = await self._request(
            "POST", path, json=payload, timeout=timeout if timeout is not None else self._timeout
        )
        return RetainResult(
            success=bool(data.get("success", False)),
            bank_id=str(data.get("bank_id", bank_id)),
            items_count=int(data.get("items_count", 0)),
            is_async=bool(data.get("async", False)),
            # 响应里可能给 operation_id（单个）或 operation_ids（数组，
            # 当 items 用了不同策略分组时）。取第一个即可：同一批的其余
            # 操作共用同一个本地批次记录，对账时以父 id 为准。
            operation_id=_first_operation_id(data),
            usage=dict(data.get("usage") or {}),
        )

    async def recall(
        self,
        bank_id: str,
        query: str,
        *,
        types: list[str] | None = None,
        budget: str | None = None,
        max_tokens: int | None = None,
        include_chunks: bool | None = None,
        max_chunk_tokens: int | None = None,
        include_entities: bool | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """按自然语言查询召回记忆。

        Args:
            bank_id: 记忆库标识。
            query: 查询语句。
            types: 只返回这些类型（``world`` / ``experience`` / ``observation``）。
                省略则全返回。
            budget: 搜索深度 ``low`` / ``mid`` / ``high``。
            max_tokens: 返回内容的 token 预算，服务端默认 4096。
            include_chunks: 是否附带生成该记忆的原始文本块。
            max_chunk_tokens: chunk 的 token 预算。
            include_entities: 是否附带实体信息。
            tags: 按可见性标签过滤。
            tags_match: 标签匹配方式 ``any`` / ``all`` / ``exact``。
            timeout: 覆盖本次请求的超时（秒）。召回会在用户每句话之前执行，
                慢了就会拖住对话，所以默认用较短的 ``RECALL_TIMEOUT``。

        Returns:
            服务端原始响应（含 ``results`` 等字段）。

        Raises:
            HindsightConnectionError: 网络不可达或超时。
            HindsightAuthError: 鉴权失败。
            HindsightRequestError: 请求被拒绝。
            HindsightServerError: 服务端 5xx 或响应不是合法 JSON。
        """
        payload = _drop_none(
            {
                "query": query,
                "types": types,
                "budget": budget,
                "max_tokens": max_tokens,
                "include_chunks": include_chunks,
                "max_chunk_tokens": max_chunk_tokens,
                "include_entities": include_entities,
                "tags": tags,
                "tags_match": tags_match,
            }
        )
        return await self._request(
            "POST",
            f"/v1/default/banks/{_quote_bank(bank_id)}/memories/recall",
            json=payload,
            timeout=timeout if timeout is not None else RECALL_TIMEOUT,
        )

    async def reflect(
        self,
        bank_id: str,
        query: str,
        *,
        budget: str | None = None,
        include_facts: bool | None = None,
    ) -> dict:
        """让记忆库结合自身人格设定推理作答。

        Args:
            bank_id: 记忆库标识。
            query: 问题。
            budget: 推理深度 ``low`` / ``mid`` / ``high``。
            include_facts: 是否在 ``based_on`` 里返回引用的记忆。

        Returns:
            服务端原始响应（``text`` / ``based_on`` / ``structured_output`` 等）。

        Raises:
            HindsightConnectionError: 网络不可达或超时。
            HindsightAuthError: 鉴权失败。
            HindsightRequestError: 请求被拒绝。
            HindsightServerError: 服务端 5xx 或响应不是合法 JSON。
        """
        payload = _drop_none(
            {"query": query, "budget": budget, "include_facts": include_facts}
        )
        return await self._post(f"/v1/default/banks/{_quote_bank(bank_id)}/reflect", payload)

    async def health(self) -> dict:
        """探测服务端就绪状态。

        Returns:
            服务端 ``/health`` 的响应体。

        Raises:
            HindsightConnectionError: 网络不可达或超时。
            HindsightServerError: 数据库不可达（503）或响应不是合法 JSON。
        """
        return await self._get("/health")

    async def bank_exists(self, bank_id: str) -> bool:
        """检查记忆库是否已存在。

        服务端没有 ``GET /v1/default/banks/{bank_id}``（对该路径只开放
        DELETE/PATCH/PUT），所以只能列出后逐个比对 ``bank_id``。
        ``q`` 是**子串**过滤，必须再做一次精确比较，否则 ``dev`` 会被
        ``dev-test`` 的存在骗过去。

        Args:
            bank_id: 记忆库标识。

        Returns:
            存在返回 True。
        """
        wanted = (bank_id or "").strip()
        if not wanted:
            raise ValueError("bank_id 不能为空")
        data = await self._get("/v1/default/banks", params={"q": wanted, "limit": 100})
        for item in data.get("banks") or []:
            if item.get("bank_id") == wanted:
                return True
        return False

    async def ensure_bank(self, bank_id: str) -> bool:
        """确保记忆库存在，不存在则以默认配置创建。

        服务端不会因为一次 retain/recall 就自动建库：召回一个空库会稳定
        返回 404 ``Bank 'xxx' not found``。首次运行的插件因此永远召回失败。

        用 ``PUT /v1/default/banks/{bank_id}`` 建库（同一个操作也用于更新，
        重复调用返回 200，天然幂等）。空 body 表示全部取服务端默认值，
        所以这里**不传任何字段**：传了反而会覆盖用户在该库上做过的配置。

        Args:
            bank_id: 记忆库标识。

        Returns:
            本次调用是否真的创建了库（已存在时为 False）。
        """
        wanted = (bank_id or "").strip()
        if await self.bank_exists(wanted):
            return False
        await self._request(
            "PUT", f"/v1/default/banks/{_quote_bank(wanted)}", json={}
        )
        return True

    async def list_operations(
        self,
        bank_id: str,
        *,
        status: str | None = None,
        type_: str | None = None,
        limit: int = 100,
        offset: int = 0,
        exclude_parents: bool = False,
    ) -> dict:
        """列出记忆库的操作记录。

        一次能拿回一整页，比逐个 ``get_operation`` 省掉 N 次往返——巡检时
        本地可能积压了几十条待确认的批次，逐条查会明显拖慢一轮同步。

        Args:
            bank_id: 记忆库标识。
            status: 只列这个状态：``pending`` / ``processing`` / ``completed``
                / ``failed`` / ``cancelled``。省略则全列。
            type_: 只列这个类型：``retain`` / ``batch_retain`` /
                ``consolidation`` / ``refresh_mental_model`` /
                ``file_convert_retain`` / ``webhook_delivery``。
            limit: 返回条数上限，服务端上限是 100。
            offset: 分页偏移。
            exclude_parents: 是否排除父操作。父操作（type=batch_retain）只是
                一组子操作的壳，它的状态跟随子操作，通常不需要单独看。

        Returns:
            服务端原始响应（``bank_id`` / ``total`` / ``limit`` /
            ``offset`` / ``operations``）。
        """
        params = _drop_none(
            {
                "status": status,
                "type": type_,
                "limit": limit,
                "offset": offset or None,
                "exclude_parents": True if exclude_parents else None,
            }
        )
        return await self._get(
            f"/v1/default/banks/{_quote_bank(bank_id)}/operations", params=params
        )

    async def get_operation(self, bank_id: str, operation_id: str) -> dict:
        """查询单个操作的当前状态。

        Args:
            bank_id: 记忆库标识。
            operation_id: 操作标识（``retain`` 响应里返回的那个）。

        Returns:
            服务端原始响应，关键字段是 ``status``（``pending`` /
            ``processing`` / ``completed`` / ``failed`` / ``cancelled`` /
            ``not_found``）与 ``error_message``。
        """
        wanted = (operation_id or "").strip()
        if not wanted:
            raise ValueError("operation_id 不能为空")
        return await self._get(
            f"/v1/default/banks/{_quote_bank(bank_id)}/operations/{quote(wanted, safe='')}"
        )

    async def retry_operation(self, bank_id: str, operation_id: str) -> dict:
        """让服务端重新执行一个失败的操作。

        这是「失败后首选的重试方式」：服务端用它已经收到的原文重跑抽取，
        不会在文档末尾重复追加；而用相同内容重新 retain 会再追加一遍，
        同一句话就进两次库。

        Args:
            bank_id: 记忆库标识。
            operation_id: 操作标识。

        Returns:
            服务端原始响应（``success`` / ``message`` / ``operation_id``）。

        Raises:
            HindsightBankNotFoundError: 操作不存在（已被清理），此时只能
                放弃这条路径、回退成重新投递。
        """
        wanted = (operation_id or "").strip()
        if not wanted:
            raise ValueError("operation_id 不能为空")
        return await self._post(
            f"/v1/default/banks/{_quote_bank(bank_id)}/operations/{quote(wanted, safe='')}/retry",
            {},
        )

    async def aclose(self) -> None:
        """关闭自建的连接池。

        传入 ``http_client`` 时本方法什么也不做——连接池的所有权在调用方手里。
        """
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        """构造请求头。

        Token 为空时**不放入** ``Authorization`` 键，而不是放空字符串：
        部分服务端会把「有头但值为空」当成格式错误的凭证直接 401。
        """
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _post(self, path: str, payload: dict) -> dict:
        """发一个 POST 并解析响应。"""
        return await self._request("POST", path, json=payload)

    async def _get(self, path: str, **kwargs) -> dict:
        """发一个 GET 并解析响应。"""
        return await self._request("GET", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """统一的请求与异常翻译。

        Args:
            method: HTTP 方法。
            path: 以 ``/`` 开头的路径。
            **kwargs: 透传给 httpx 的参数（如 ``json=``）。

        Returns:
            解析后的 JSON 对象。

        Raises:
            HindsightConnectionError: 网络层失败。
            HindsightAuthError / HindsightRequestError / HindsightServerError:
                按状态码分类的业务失败。
        """
        url = f"{self._base_url}{path}"
        # 允许单次请求覆盖超时：异步提交几秒返回，同步提交要等几十秒，
        # 召回又必须几秒内放弃，一个全局值不可能同时合适。
        effective_timeout = kwargs.pop("timeout", None) or self._timeout
        try:
            response = await self._http.request(
                method, url, headers=self._headers, timeout=effective_timeout, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise HindsightConnectionError(
                f"请求超时（{effective_timeout:g} 秒）：{url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HindsightConnectionError(f"无法连接 Hindsight（{url}）：{exc}") from exc

        return self._parse(response, url, path=path)

    def _parse(self, response: httpx.Response, url: str, *, path: str = "") -> dict:
        """按状态码把响应翻译成返回值或异常。

        Args:
            response: httpx 响应对象。
            url: 完整地址，仅用于错误信息。
            path: 请求路径。用来区分同一个 404 的两种含义：库不存在，还是
                操作不存在——两者的补救办法完全不同。

        Returns:
            响应 JSON。

        Raises:
            HindsightAuthError: 401 / 403。
            HindsightBankNotFoundError: 404 且路径指向记忆库。
            HindsightOperationNotFoundError: 404 且路径指向操作。
            HindsightRequestError: 其余 4xx。
            HindsightServerError: 5xx 或非 JSON 响应。
        """
        status = response.status_code
        detail = _extract_detail(response)

        if status == 404:
            if "/operations/" in path:
                raise HindsightOperationNotFoundError(
                    f"操作不存在（HTTP 404，可能已被服务端清理）：{detail}"
                )
            # 业务路径全部挂在 bank 下，404 的语义就是「这个库没有」。
            raise HindsightBankNotFoundError(
                f"记忆库不存在（HTTP 404）：{detail}"
            )
        if status in (401, 403):
            raise HindsightAuthError(
                f"鉴权失败（HTTP {status}）：{detail}", token_present=self.token_present
            )
        if status >= 500:
            raise HindsightServerError(f"服务端错误（HTTP {status}）：{detail}")
        if status >= 400:
            raise HindsightRequestError(f"请求被拒绝（HTTP {status}）：{detail}")

        try:
            return response.json()
        except ValueError as exc:
            raise HindsightServerError(
                f"响应不是合法 JSON（{url}）：{response.text[:200]}"
            ) from exc


# ----------------------------------------------------------------------
# 模块级工具函数（纯函数，便于单测）
# ----------------------------------------------------------------------


def _normalize_base_url(base_url: str) -> str:
    """规范化服务地址：补 scheme、去尾部斜杠。

    Args:
        base_url: 原始地址。

    Returns:
        形如 ``http://localhost:8888`` 的地址。
    """
    value = (base_url or "").strip().rstrip("/")
    if not value:
        raise ValueError("base_url 不能为空")
    if "://" not in value:
        value = f"http://{value}"
    return value


def _has_explicit_port(url: str) -> bool:
    """判断 URL 的 authority 部分是否已经写了端口。

    Args:
        url: 已带 scheme 的 URL。

    Returns:
        存在显式端口时返回 True。
    """
    authority = url.split("://", 1)[-1].split("/", 1)[0]
    # IPv6 字面量形如 [::1]:8888，冒号出现在方括号内不算端口分隔符。
    if authority.startswith("["):
        _, _, rest = authority.partition("]")
        return rest.startswith(":")
    return ":" in authority


def _quote_bank(bank_id: str) -> str:
    """对 bank_id 做 URL 路径编码。

    bank_id 由调用方自定，可能含 ``/`` 或空格，直接拼进路径会把层级搞乱。

    Args:
        bank_id: 记忆库标识。

    Returns:
        编码后的路径段。
    """
    value = (bank_id or "").strip()
    if not value:
        raise ValueError("bank_id 不能为空")
    return quote(value, safe="")


def _drop_none(mapping: dict) -> dict:
    """去掉值为 None 的键。

    服务端对显式的 ``null`` 与「不提供」的处理可能不同，省略更安全。

    Args:
        mapping: 待过滤的字典。

    Returns:
        仅保留非 None 值的新字典。
    """
    return {key: value for key, value in mapping.items() if value is not None}


def _first_operation_id(data: dict) -> str:
    """从 retain 响应里取出操作标识。

    服务端在 ``async=true`` 时返回 ``operation_id`` 字符串；当 items 使用了
    不同的 per-item 策略、被拆成多组时，改用它给出 ``operation_ids`` 数组。
    两种都兼容，取第一个即可——本项目的 items 都是同一策略，实际只会有一个。

    Args:
        data: retain 响应的 JSON。

    Returns:
        操作标识；同步模式或字段缺失时返回空串。
    """
    single = data.get("operation_id")
    if isinstance(single, str) and single.strip():
        return single.strip()
    many = data.get("operation_ids")
    if isinstance(many, list):
        for item in many:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _extract_detail(response: httpx.Response) -> str:
    """从错误响应里提取可读信息。

    FastAPI 的校验错误把细节放在 ``detail`` 里（可能是字符串或列表），
    不是 JSON 时退回响应文本。

    Args:
        response: httpx 响应对象。

    Returns:
        供日志与提示使用的简短描述。
    """
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:200] or "（无响应体）"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)[:200]
    return str(payload)[:200]
