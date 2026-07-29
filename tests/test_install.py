import pytest
from fastmcp import FastMCP, Client
from prometheus_client import CollectorRegistry

from mcpfleet_obs import install
from mcpfleet_obs.metrics import REGION, TRANSPORT


@pytest.mark.asyncio
async def test_install_wires_metrics_route_and_connection_counter():
    registry = CollectorRegistry()
    mcp = FastMCP(name="t")
    install(mcp, prefix="wired", registry=registry)

    @mcp.tool()
    def ok() -> str:
        return "x"

    async with Client(mcp) as c:
        await c.call_tool_mcp("ok", {})

    # The ok-path tool call must record exactly one sample, on the
    # ok/no-error-category labels — a substring check on the `# HELP` line
    # would pass even with zero recorded samples, since that text is emitted
    # regardless of whether the counter was ever incremented.
    tool_calls_ok = registry.get_sample_value(
        "wired_tool_calls_total",
        {
            "tool": "ok",
            "transport": TRANSPORT,
            "region": REGION,
            "status": "ok",
            "error_category": "",
        },
    )
    assert tool_calls_ok == 1.0

    # in-process fastmcp.Client defaults clientInfo to name="mcp",
    # version="0.1.0" (mcp.client.session.DEFAULT_CLIENT_INFO) when no
    # explicit client_info is passed — see clients.py's on_request handler,
    # which reads clientInfo off the initialize request params.
    connections_total = sum(
        sample.value
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name == "wired_client_connections_total"
    )
    assert connections_total >= 1.0
