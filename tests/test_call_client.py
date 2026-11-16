"""Per-call client attribution: resolve_call_client + resolve_ip_hash.

The fleet runs stateless_http=True, so a tools/call carries no session
identity — resolution must read what the request itself offers, in order:
request _meta clientInfo → session client_params → User-Agent → "unknown".
"""
from types import SimpleNamespace

import pytest

import mcpfleet_obs.clients as clients
from mcpfleet_obs.clients import (
    CLIENT_INFO_META_KEY,
    _BoundedLabel,
    resolve_call_client,
    resolve_ip_hash,
)

META = CLIENT_INFO_META_KEY


def _ctx(**message_kw):
    """Bare on_call_tool context: message kwargs + no fastmcp_context."""
    return SimpleNamespace(message=SimpleNamespace(**message_kw))


def _labels(n=40):
    return _BoundedLabel(n)


def test_meta_client_info_wins():
    ctx = _ctx(name="t", meta={META: {"name": "modern-client", "version": "1"}})
    assert resolve_call_client(ctx, _labels()) == "modern-client"


def test_meta_pydantic_extra_reads_model_extra():
    """SDK 1.x params.meta is a pydantic model; extra keys land in model_extra."""
    meta = SimpleNamespace(model_extra={META: {"name": "meta-client"}})
    ctx = _ctx(name="t", meta=meta)
    assert resolve_call_client(ctx, _labels()) == "meta-client"


def test_session_client_params_when_no_meta():
    ctx = _ctx(name="t")
    ctx.fastmcp_context = SimpleNamespace(
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    client_info=SimpleNamespace(name="session-client")))))
    assert resolve_call_client(ctx, _labels()) == "session-client"


def test_session_camel_case_sdk_1x():
    ctx = _ctx(name="t")
    ctx.fastmcp_context = SimpleNamespace(
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    clientInfo=SimpleNamespace(name="legacy-client")))))
    assert resolve_call_client(ctx, _labels()) == "legacy-client"


def test_meta_beats_session():
    """A client that stamps _meta AND has a session: per-request stamp wins."""
    ctx = _ctx(name="t", meta={META: {"name": "meta-wins"}})
    ctx.fastmcp_context = SimpleNamespace(
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    client_info=SimpleNamespace(name="session-loses")))))
    assert resolve_call_client(ctx, _labels()) == "meta-wins"


def test_user_agent_fallback_prefixed(monkeypatch):
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={"user-agent": "python-httpx/0.28.1"}))
    assert resolve_call_client(_ctx(name="t"), _labels()) == "ua:python-httpx/0.28.1"


def test_user_agent_first_token_only(monkeypatch):
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={"user-agent": "Mozilla/5.0 (X11; Linux)"}))
    assert resolve_call_client(_ctx(name="t"), _labels()) == "ua:Mozilla/5.0"


def test_no_identity_is_unknown(monkeypatch):
    def _boom():
        raise RuntimeError("no request")
    monkeypatch.setattr(clients, "get_http_request", _boom)
    assert resolve_call_client(_ctx(name="t"), _labels()) == "unknown"


def test_empty_ua_is_unknown(monkeypatch):
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={}))
    assert resolve_call_client(_ctx(name="t"), _labels()) == "unknown"


def test_bounded_label_folds_new_clients():
    labels = _labels(1)
    assert resolve_call_client(_ctx(name="t", meta={META: {"name": "first"}}), labels) == "first"
    assert resolve_call_client(_ctx(name="t", meta={META: {"name": "second"}}), labels) == "other"


# --- ip_hash -------------------------------------------------------------

def _ip_env(monkeypatch, salt=b"sixteen-byte-sal"):
    monkeypatch.setattr(clients, "IP_LABEL_ENABLED", True)
    monkeypatch.setattr(clients, "_IP_SALT", salt)


def test_ip_hash_disabled_by_default(monkeypatch):
    monkeypatch.setattr(clients, "IP_LABEL_ENABLED", False)
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={"fly-client-ip": "1.2.3.4"}))
    assert resolve_ip_hash() == ""


def test_ip_hash_stable_within_process(monkeypatch):
    _ip_env(monkeypatch)
    req = lambda: SimpleNamespace(headers={"fly-client-ip": "1.2.3.4"})
    monkeypatch.setattr(clients, "get_http_request", req)
    h = resolve_ip_hash()
    assert len(h) == 12 and h == resolve_ip_hash()


def test_ip_hash_differs_per_ip(monkeypatch):
    _ip_env(monkeypatch)
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={"fly-client-ip": "1.2.3.4"}))
    a = resolve_ip_hash()
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers={"fly-client-ip": "5.6.7.8"}))
    assert a != resolve_ip_hash()


def test_ip_hash_header_precedence(monkeypatch):
    """cf-connecting-ip wins when fronted; fly-client-ip beats spoofable xff."""
    _ip_env(monkeypatch)
    headers = {"cf-connecting-ip": "9.9.9.9", "fly-client-ip": "1.1.1.1",
               "x-forwarded-for": "2.2.2.2, 3.3.3.3"}
    monkeypatch.setattr(clients, "get_http_request",
                        lambda: SimpleNamespace(headers=headers))
    import hashlib
    assert resolve_ip_hash() == hashlib.sha256(b"sixteen-byte-sal" + b"9.9.9.9").hexdigest()[:12]

    del headers["cf-connecting-ip"]
    assert resolve_ip_hash() == hashlib.sha256(b"sixteen-byte-sal" + b"1.1.1.1").hexdigest()[:12]

    del headers["fly-client-ip"]
    assert resolve_ip_hash() == hashlib.sha256(b"sixteen-byte-sal" + b"2.2.2.2").hexdigest()[:12]


def test_ip_hash_no_request_is_empty(monkeypatch):
    _ip_env(monkeypatch)
    def _boom():
        raise RuntimeError("no request")
    monkeypatch.setattr(clients, "get_http_request", _boom)
    assert resolve_ip_hash() == ""


# --- end to end over the wire --------------------------------------------

@pytest.mark.asyncio
async def test_http_call_carries_ua_label():
    """A stateless tools/call with no _meta and no session resolves through
    the request's User-Agent — the only signal legacy clients leave."""
    from fastmcp import FastMCP
    from prometheus_client import CollectorRegistry
    from starlette.testclient import TestClient

    from mcpfleet_obs import install

    registry = CollectorRegistry()
    mcp = FastMCP(name="t")
    install(mcp, prefix="uacall", registry=registry)

    @mcp.tool()
    def ping() -> str:
        return "pong"

    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json",
               "User-Agent": "scrape-script/9.9"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ping", "arguments": {}}}
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True,
                                 json_response=True)) as tc:
        assert tc.post("/mcp", json=body, headers=headers).status_code == 200

    value = registry.get_sample_value(
        "uacall_tool_calls_total",
        {"tool": "ping", "transport": "http", "region": "local",
         "status": "ok", "error_category": "", "source": "",
         "client": "ua:scrape-script/9.9", "ip_hash": ""})
    assert value == 1.0


@pytest.mark.asyncio
async def test_http_call_meta_clientinfo_beats_ua():
    """A modern-protocol client stamps clientInfo in params._meta on every
    request — the transport's lifted _meta reaches middleware and must win
    over the User-Agent fallback."""
    from fastmcp import FastMCP
    from prometheus_client import CollectorRegistry
    from starlette.testclient import TestClient

    from mcpfleet_obs import install

    registry = CollectorRegistry()
    mcp = FastMCP(name="t")
    install(mcp, prefix="metacall", registry=registry)

    @mcp.tool()
    def ping() -> str:
        return "pong"

    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json",
               "User-Agent": "some-library/1.0"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ping", "arguments": {},
                       "_meta": {META: {"name": "modern-caller", "version": "2.0"}}}}
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True,
                                 json_response=True)) as tc:
        assert tc.post("/mcp", json=body, headers=headers).status_code == 200

    value = registry.get_sample_value(
        "metacall_tool_calls_total",
        {"tool": "ping", "transport": "http", "region": "local",
         "status": "ok", "error_category": "", "source": "",
         "client": "modern-caller", "ip_hash": ""})
    assert value == 1.0
