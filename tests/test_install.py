import pytest
from fastmcp import FastMCP, Client
from prometheus_client import CollectorRegistry, generate_latest

from mcpfleet_obs import install


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

    text = generate_latest(registry).decode()
    assert "wired_tool_calls_total" in text
    assert "wired_client_connections_total" in text  # in-process init counted
