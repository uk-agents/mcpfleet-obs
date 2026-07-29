import json
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError, ToolError
from prometheus_client import CollectorRegistry

from mcpfleet_obs.errors import raise_tool_error
from mcpfleet_obs.metrics import FleetMetricsMiddleware, make_metrics


def _ctx(tool="my_tool"):
    return SimpleNamespace(message=SimpleNamespace(name=tool))


def _value(registry, name, **labels):
    return registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
def mw():
    registry = CollectorRegistry()
    metrics = make_metrics("testfleet", registry=registry)
    return FleetMetricsMiddleware(metrics), registry


async def test_ok_result_counts_ok(mw):
    middleware, registry = mw
    async def call_next(ctx):
        return SimpleNamespace()  # 3.2.x results have no is_error attr
    await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="ok", error_category="") == 1.0


async def test_exception_counts_error_with_category(mw):
    middleware, registry = mw
    async def call_next(ctx):
        raise_tool_error("transient", is_retryable=True, attempted="t()", description="d")
    with pytest.raises(ToolError):
        await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="transient") == 1.0


async def test_is_error_result_counts_error(mw):
    """P3: on fastmcp 3.4.x a PROXIED failure arrives as a result with
    is_error=True and no exception. Exception-only counting marks it ok."""
    middleware, registry = mw
    async def call_next(ctx):
        return SimpleNamespace(is_error=True)
    await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="unknown") == 1.0


async def test_unknown_tool_bucketed(mw):
    """Scanner cardinality fix: NotFoundError => tool label '_unknown_'."""
    middleware, registry = mw
    async def call_next(ctx):
        raise NotFoundError("Unknown tool: __verifymcp_probe_abc123__")
    with pytest.raises(NotFoundError):
        await middleware.on_call_tool(_ctx("__verifymcp_probe_abc123__"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="_unknown_", transport="http", region="local",
                  status="error", error_category="not_found") == 1.0


async def test_duration_observed(mw):
    middleware, registry = mw
    async def call_next(ctx):
        return SimpleNamespace()
    await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_duration_seconds_count",
                  tool="my_tool", transport="http", region="local") == 1.0


async def test_integration_real_server_exception_path():
    from fastmcp import FastMCP, Client
    registry = CollectorRegistry()
    metrics = make_metrics("integ", registry=registry)
    mcp = FastMCP(name="integ")
    mcp.add_middleware(FleetMetricsMiddleware(metrics))

    @mcp.tool()
    def bad() -> str:
        raise_tool_error("transient", is_retryable=True, attempted="bad()", description="d")

    async with Client(mcp) as c:
        await c.call_tool_mcp("bad", {})
    assert _value(registry, "integ_tool_calls_total",
                  tool="bad", transport="http", region="local",
                  status="error", error_category="transient") == 1.0
