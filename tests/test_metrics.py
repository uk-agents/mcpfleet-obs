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


async def test_is_error_result_with_payload_extracts_category(mw):
    """is_error=True with populated content[0].text containing FleetErrorPayload
    JSON should parse and extract error_category correctly."""
    from mcpfleet_obs.errors import FleetErrorPayload
    middleware, registry = mw
    payload_json = FleetErrorPayload(
        error_category="transient", is_retryable=True,
        attempted="t()", description="d").model_dump_json()
    async def call_next(ctx):
        return SimpleNamespace(
            is_error=True,
            content=[SimpleNamespace(text=payload_json)])
    await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="transient") == 1.0


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


async def test_fastmcp_validation_error_counts_as_validation(mw):
    """FastMCP validates arguments against the tool signature BEFORE the body
    runs. 3.3+ raises its own ValidationError for that; without an explicit
    branch it falls through to the BaseException catch-all and is recorded as
    'unknown', conflating agent input mistakes with genuine upstream faults."""
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    middleware, registry = mw

    async def call_next(ctx):
        raise FastMCPValidationError("1 validation error for call[my_tool]")

    with pytest.raises(FastMCPValidationError):
        await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="validation") == 1.0


async def test_pydantic_argument_error_counts_as_validation(mw):
    """On fastmcp 3.2.x the same rejection arrives as pydantic's own
    ValidationError, identifiable only by its 'call[<tool>]' title."""
    from pydantic import ValidationError as PydanticValidationError

    middleware, registry = mw
    err = PydanticValidationError.from_exception_data(
        "call[my_tool]",
        [{"type": "greater_than_equal", "loc": ("n",), "input": 0,
          "ctx": {"ge": 1}}])

    async def call_next(ctx):
        raise err

    with pytest.raises(PydanticValidationError):
        await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="validation") == 1.0


async def test_body_raised_validation_error_stays_unknown(mw):
    """A tool body constructing a bad model raises the SAME pydantic class.
    That is a server bug, not caller input — mislabelling it 'validation'
    would hide it behind the agents' own mistakes."""
    from pydantic import ValidationError as PydanticValidationError

    middleware, registry = mw
    err = PydanticValidationError.from_exception_data(
        "UpstreamResponse",
        [{"type": "greater_than_equal", "loc": ("n",), "input": 0,
          "ctx": {"ge": 1}}])

    async def call_next(ctx):
        raise err

    with pytest.raises(PydanticValidationError):
        await middleware.on_call_tool(_ctx(), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="unknown") == 1.0
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="my_tool", transport="http", region="local",
                  status="error", error_category="validation") == 0.0


async def test_integration_bad_argument_counts_as_validation():
    """End-to-end: a constraint violation on a real server must land in
    'validation', not 'unknown'."""
    from typing import Annotated

    from fastmcp import Client, FastMCP
    from pydantic import Field

    registry = CollectorRegistry()
    metrics = make_metrics("integv", registry=registry)
    mcp = FastMCP(name="integv")
    mcp.add_middleware(FleetMetricsMiddleware(metrics))

    @mcp.tool()
    def search(
        items_per_page: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> str:
        return "ok"

    async with Client(mcp) as c:
        await c.call_tool_mcp("search", {"items_per_page": 500})
    assert _value(registry, "integv_tool_calls_total",
                  tool="search", transport="http", region="local",
                  status="error", error_category="validation") == 1.0
