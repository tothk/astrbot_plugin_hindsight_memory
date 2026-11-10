"""hindsight/client.py 的单元测试。

不连真实服务：用 httpx 的 MockTransport 把请求截下来，既能断言
"发了什么"，也能造出任何状态码。
"""

import httpx
import pytest

from astrbot_plugin_hindsight_memory.hindsight.client import (
    HindsightAuthError,
    HindsightClient,
    HindsightConnectionError,
    HindsightRequestError,
    HindsightServerError,
    _drop_none,
    _extract_detail,
    _has_explicit_port,
    _normalize_base_url,
    _quote_bank,
)


class Recorder:
    """记录经过 MockTransport 的请求，并按预设返回响应。"""

    def __init__(self, responses: dict[str, httpx.Response] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.responses.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx.Response(200, json={})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(recorder: Recorder, token: str = "") -> HindsightClient:
    """用 MockTransport 造一个不会真的发请求的客户端。"""
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return HindsightClient("http://localhost:8888", token=token, http_client=http)


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


class TestNormalizeBaseUrl:
    def test_adds_scheme_when_missing(self):
        assert _normalize_base_url("localhost:8888") == "http://localhost:8888"

    def test_keeps_existing_scheme(self):
        assert _normalize_base_url("https://api.example.com") == "https://api.example.com"

    def test_strips_trailing_slash(self):
        assert _normalize_base_url("http://1.2.3.4:9000/") == "http://1.2.3.4:9000"

    def test_strips_whitespace(self):
        assert _normalize_base_url("  localhost:8888  ") == "http://localhost:8888"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _normalize_base_url("")


class TestHasExplicitPort:
    def test_with_port(self):
        assert _has_explicit_port("http://localhost:8888") is True

    def test_without_port(self):
        assert _has_explicit_port("http://localhost") is False

    def test_with_path(self):
        assert _has_explicit_port("http://localhost:8888/api") is True

    def test_ipv6_with_port(self):
        assert _has_explicit_port("http://[::1]:8888") is True

    def test_ipv6_without_port(self):
        assert _has_explicit_port("http://[::1]") is False


class TestQuoteBank:
    def test_plain(self):
        assert _quote_bank("my-bank") == "my-bank"

    def test_slash_is_encoded(self):
        # bank_id 里的 / 若原样拼进路径，会变成多一级路由
        assert _quote_bank("a/b") == "a%2Fb"

    def test_space_is_encoded(self):
        assert _quote_bank("my bank") == "my%20bank"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _quote_bank("  ")


class TestDropNone:
    def test_removes_none_values(self):
        assert _drop_none({"a": 1, "b": None}) == {"a": 1}

    def test_keeps_falsy_but_not_none(self):
        # False / 0 / "" 都是有意义的值，不能被当成 None 丢掉
        assert _drop_none({"a": False, "b": 0, "c": ""}) == {"a": False, "b": 0, "c": ""}


class TestExtractDetail:
    def test_string_detail(self):
        response = httpx.Response(422, json={"detail": "field required"})
        assert _extract_detail(response) == "field required"

    def test_list_detail_is_stringified(self):
        response = httpx.Response(422, json={"detail": [{"loc": ["body"], "msg": "bad"}]})
        assert "bad" in _extract_detail(response)

    def test_non_json_falls_back_to_text(self):
        response = httpx.Response(500, text="internal error")
        assert _extract_detail(response) == "internal error"

    def test_empty_body(self):
        response = httpx.Response(500, text="")
        assert _extract_detail(response) == "（无响应体）"


# ----------------------------------------------------------------------
# 构造与请求头
# ----------------------------------------------------------------------


class TestFromConfig:
    def test_builds_url_from_host_and_port(self):
        client = HindsightClient.from_config("localhost", 8888)
        assert client.base_url == "http://localhost:8888"

    def test_host_with_scheme(self):
        client = HindsightClient.from_config("http://127.0.0.1", 9999)
        assert client.base_url == "http://127.0.0.1:9999"

    def test_host_with_port_wins(self):
        client = HindsightClient.from_config("http://localhost:9000", 8888)
        assert client.base_url == "http://localhost:9000"


class TestHeaders:
    def test_empty_token_omits_header_entirely(self):
        """空 token 时不能有 Authorization 键，连空值都不行。

        部分服务端把"有头但值为空"判为格式错误的凭证，直接 401。
        """
        client = HindsightClient("http://localhost:8888", token="")
        assert "Authorization" not in client._headers
        assert client.token_present is False

    def test_none_token_omits_header(self):
        client = HindsightClient("http://localhost:8888", token=None)
        assert "Authorization" not in client._headers

    def test_blank_token_is_treated_as_empty(self):
        client = HindsightClient("http://localhost:8888", token="   ")
        assert "Authorization" not in client._headers

    def test_token_is_sent_as_bearer(self):
        client = HindsightClient("http://localhost:8888", token="abc")
        assert client._headers["Authorization"] == "Bearer abc"
        assert client.token_present is True

    def test_token_is_stripped(self):
        client = HindsightClient("http://localhost:8888", token="  abc  ")
        assert client._headers["Authorization"] == "Bearer abc"


# ----------------------------------------------------------------------
# retain
# ----------------------------------------------------------------------


class TestRetain:
    async def test_posts_items_to_memories_endpoint(self):
        rec = Recorder({"/memories": httpx.Response(200, json={"success": True})})
        client = make_client(rec)
        await client.retain("my-bank", [{"content": "hi"}])

        assert rec.last.method == "POST"
        assert rec.last.url.path == "/v1/default/banks/my-bank/memories"
        assert b'"content":"hi"' in rec.last.content

    async def test_parses_response(self):
        rec = Recorder(
            {
                "/memories": httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bank_id": "b1",
                        "items_count": 3,
                        "async": True,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                )
            }
        )
        result = await make_client(rec).retain("b1", [{"content": "x"}])

        assert result.success is True
        assert result.bank_id == "b1"
        assert result.items_count == 3
        assert result.is_async is True
        assert result.usage["total_tokens"] == 15

    async def test_missing_usage_becomes_empty_dict(self):
        rec = Recorder({"/memories": httpx.Response(200, json={"success": True})})
        result = await make_client(rec).retain("b1", [{"content": "x"}])
        assert result.usage == {}

    async def test_empty_items_rejected_locally(self):
        """没有待提交内容时不该真的发请求，否则会拿到 422 被当成故障。"""
        rec = Recorder()
        with pytest.raises(ValueError):
            await make_client(rec).retain("b1", [])
        assert rec.requests == []

    async def test_bank_id_is_url_encoded(self):
        # 注意看 str(url) 而不是 url.path：httpx 的 .path 会把 %2F 解码回 /，
        # 看不出实际发出去的字节。
        rec = Recorder({"/memories": httpx.Response(200, json={})})
        await make_client(rec).retain("a/b", [{"content": "x"}])
        assert "/banks/a%2Fb/" in str(rec.last.url)

    async def test_sends_append_mode_payload_intact(self):
        """append 语义完全由 payload 表达，客户端不能改写它。"""
        rec = Recorder({"/memories": httpx.Response(200, json={})})
        item = {"content": "x", "document_id": "d1", "update_mode": "append"}
        await make_client(rec).retain("b1", [item])
        assert b'"update_mode":"append"' in rec.last.content
        assert b'"document_id":"d1"' in rec.last.content


# ----------------------------------------------------------------------
# recall / reflect
# ----------------------------------------------------------------------


class TestRecall:
    async def test_drops_none_params(self):
        rec = Recorder({"/recall": httpx.Response(200, json={"results": []})})
        await make_client(rec).recall("b1", "q")
        assert rec.last.content == b'{"query":"q"}'

    async def test_keeps_falsey_params(self):
        rec = Recorder({"/recall": httpx.Response(200, json={"results": []})})
        await make_client(rec).recall("b1", "q", include_chunks=False, max_tokens=0)
        assert b'"include_chunks":false' in rec.last.content
        assert b'"max_tokens":0' in rec.last.content

    async def test_passes_all_options(self):
        rec = Recorder({"/recall": httpx.Response(200, json={"results": []})})
        await make_client(rec).recall(
            "b1",
            "q",
            types=["world"],
            budget="high",
            max_tokens=2048,
            tags=["user:1"],
            tags_match="all",
        )
        body = rec.last.content
        for fragment in (b'"budget":"high"', b'"max_tokens":2048', b'"tags_match":"all"'):
            assert fragment in body


class TestReflect:
    async def test_posts_to_reflect_endpoint(self):
        rec = Recorder({"/reflect": httpx.Response(200, json={"text": "ok"})})
        result = await make_client(rec).reflect("b1", "为什么？")
        assert rec.last.url.path == "/v1/default/banks/b1/reflect"
        assert result["text"] == "ok"


# ----------------------------------------------------------------------
# 异常翻译
# ----------------------------------------------------------------------


class TestErrorTranslation:
    async def test_401_becomes_auth_error(self):
        rec = Recorder({"/memories": httpx.Response(401, json={"detail": "nope"})})
        with pytest.raises(HindsightAuthError) as exc:
            await make_client(rec).retain("b1", [{"content": "x"}])
        assert exc.value.token_present is False

    async def test_403_with_token_marks_token_present(self):
        rec = Recorder({"/memories": httpx.Response(403, json={"detail": "expired"})})
        with pytest.raises(HindsightAuthError) as exc:
            await make_client(rec, token="abc").retain("b1", [{"content": "x"}])
        assert exc.value.token_present is True

    async def test_422_becomes_request_error(self):
        rec = Recorder({"/memories": httpx.Response(422, json={"detail": "bad"})})
        with pytest.raises(HindsightRequestError):
            await make_client(rec).retain("b1", [{"content": "x"}])

    async def test_503_becomes_server_error(self):
        rec = Recorder({"/memories": httpx.Response(503, text="db down")})
        with pytest.raises(HindsightServerError):
            await make_client(rec).retain("b1", [{"content": "x"}])

    async def test_non_json_success_becomes_server_error(self):
        rec = Recorder({"/memories": httpx.Response(200, text="<html>")})
        with pytest.raises(HindsightServerError):
            await make_client(rec).retain("b1", [{"content": "x"}])

    async def test_timeout_becomes_connection_error(self):
        def boom(request):
            raise httpx.ReadTimeout("too slow", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        client = HindsightClient("http://localhost:8888", http_client=http)
        with pytest.raises(HindsightConnectionError):
            await client.retain("b1", [{"content": "x"}])

    async def test_connect_error_becomes_connection_error(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        client = HindsightClient("http://localhost:8888", http_client=http)
        with pytest.raises(HindsightConnectionError):
            await client.retain("b1", [{"content": "x"}])


# ----------------------------------------------------------------------
# 连接池所有权
# ----------------------------------------------------------------------


class TestClientOwnership:
    async def test_aclose_does_not_close_injected_client(self):
        rec = Recorder()
        http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
        client = HindsightClient("http://localhost:8888", http_client=http)

        await client.aclose()

        assert http.is_closed is False
        await http.aclose()

    async def test_aclose_closes_own_client(self):
        client = HindsightClient("http://localhost:8888")
        await client.aclose()
        assert client._http.is_closed is True
