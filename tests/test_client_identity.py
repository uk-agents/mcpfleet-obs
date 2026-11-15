"""Client identity must be read from whichever shape the installed MCP SDK uses."""

from types import SimpleNamespace

from mcpfleet_obs.clients import _BoundedLabel, _client_identity

INFO = {"name": "named-client", "version": "7.1"}
META = "io.modelcontextprotocol/clientInfo"


def _ctx(method, params):
    return SimpleNamespace(method=method, message=SimpleNamespace(params=params))


def test_initialize_reads_client_info_snake_case_sdk_2x():
    params = SimpleNamespace(client_info=SimpleNamespace(**INFO))
    assert _client_identity(_ctx("initialize", params)) == ("named-client", "7.1")


def test_initialize_reads_clientinfo_camel_case_sdk_1x():
    params = SimpleNamespace(clientInfo=SimpleNamespace(**INFO))
    assert _client_identity(_ctx("initialize", params)) == ("named-client", "7.1")


def test_initialize_reads_a_plain_dict():
    assert _client_identity(_ctx("initialize", {"clientInfo": INFO})) == ("named-client", "7.1")


def test_server_discover_reads_client_info_from_meta():
    assert _client_identity(_ctx("server/discover", {"meta": {META: INFO}})) == ("named-client", "7.1")


def test_a_client_that_sends_nothing_is_unknown():
    assert _client_identity(_ctx("initialize", SimpleNamespace())) == ("unknown", "unknown")
    assert _client_identity(_ctx("server/discover", {})) == ("unknown", "unknown")


def test_legacy_initialize_over_http_is_counted_under_the_clients_own_name():
    """The path production clients use: a raw initialize POST through the real
    transport and middleware. The in-process fastmcp.Client does not cover it,
    because on FastMCP 4 it connects with server/discover instead."""
    from fastmcp import FastMCP
    from prometheus_client import CollectorRegistry
    from starlette.testclient import TestClient

    from mcpfleet_obs import install

    registry = CollectorRegistry()
    mcp = FastMCP(name="t")
    install(mcp, prefix="wiredhttp", registry=registry)
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "legacy-http-client", "version": "2.0"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True, json_response=True)) as tc:
        assert tc.post("/mcp", json=body, headers=headers).status_code == 200

    names = {
        s.labels["client_name"]
        for metric in registry.collect()
        for s in metric.samples
        if s.name == "wiredhttp_client_connections_total"
    }
    assert names == {"legacy-http-client"}, names


def test_bounded_label_keeps_the_first_limit_names():
    labels = _BoundedLabel(2)
    assert labels.resolve("a") == "a"
    assert labels.resolve("b") == "b"
    assert labels.resolve("c") == "other"
    # Repeat sightings keep their resolved label — "other" doesn't unsee "a".
    assert labels.resolve("a") == "a"
    assert labels.resolve("d") == "other"


def test_bounded_label_zero_limit_folds_everything():
    labels = _BoundedLabel(0)
    assert labels.resolve("a") == "other"


def test_client_names_past_the_limit_fold_to_other_over_http():
    """A client-chosen name is an unbounded series source: N+1 distinct names
    must mint at most N+1 label values, and a folded client's version must
    not mint a series per version string."""
    from fastmcp import FastMCP
    from prometheus_client import CollectorRegistry
    from starlette.testclient import TestClient

    from mcpfleet_obs import install

    registry = CollectorRegistry()
    mcp = FastMCP(name="t")
    install(mcp, prefix="boundclient", client_label_max=2, registry=registry)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def init(name, version):
        return {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": name, "version": version}},
        }

    with TestClient(mcp.http_app(path="/mcp", stateless_http=True, json_response=True)) as tc:
        for body in (init("c1", "1.0"), init("c2", "2.0"), init("c3", "3.0"), init("c4", "4.0")):
            assert tc.post("/mcp", json=body, headers=headers).status_code == 200

    label_pairs = {
        (s.labels["client_name"], s.labels["client_version"])
        for metric in registry.collect()
        for s in metric.samples
        if s.name == "boundclient_client_connections_total"
    }
    assert label_pairs == {("c1", "1.0"), ("c2", "2.0"), ("other", "other")}, label_pairs
