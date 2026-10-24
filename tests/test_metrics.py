from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError, ToolError
from prometheus_client import CollectorRegistry

from mcpfleet_obs.errors import raise_tool_error
from mcpfleet_obs.metrics import FleetMetricsMiddleware, make_metrics


def _ctx(tool="my_tool"):
    return SimpleNamespace(message=SimpleNamespace(name=tool))


def _value(registry, name, *, source="tool", **labels):
    """source defaults to "tool" — the local-domain case most tests exercise."""
    if "tool_calls_total" in name:
        labels["source"] = source
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
                  status="ok", error_category="", source="") == 1.0


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
                  status="error", error_category="not_found",
                  source="middleware") == 1.0


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
                  status="error", error_category="validation",
                  source="middleware") == 1.0


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
                  status="error", error_category="validation",
                  source="middleware") == 1.0


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
                  status="error", error_category="validation",
                  source="middleware") == 1.0


# --- source attribution -------------------------------------------------
# Three causes, three owners. The label is derived from mount configuration,
# never from the shape of the exception.


@pytest.fixture
def proxy_mw():
    """A middleware configured as uk-business is: four proxy namespaces."""
    registry = CollectorRegistry()
    metrics = make_metrics("testfleet", registry=registry)
    return FleetMetricsMiddleware(
        metrics, proxy_namespaces=("gov", "law", "dd", "prop")), registry


async def test_proxied_tool_error_sourced_upstream(proxy_mw):
    middleware, registry = proxy_mw

    async def call_next(ctx):
        raise ToolError("1 validation error for call[govuk_search]")

    with pytest.raises(ToolError):
        await middleware.on_call_tool(_ctx("gov_govuk_search"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="gov_govuk_search", transport="http", region="local",
                  status="error", error_category="unknown",
                  source="upstream") == 1.0


async def test_local_tool_error_sourced_tool_on_a_proxy_server(proxy_mw):
    """A server with proxy mounts still has local tools; they are not upstream."""
    middleware, registry = proxy_mw

    async def call_next(ctx):
        raise_tool_error("transient", is_retryable=True, attempted="t()", description="d")

    with pytest.raises(ToolError):
        await middleware.on_call_tool(_ctx("health_check"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="health_check", transport="http", region="local",
                  status="error", error_category="transient",
                  source="tool") == 1.0


async def test_namespace_match_is_prefix_scoped(proxy_mw):
    """'government_report' must not match the 'gov' namespace — the separator
    is part of the prefix, so only 'gov_*' is upstream."""
    middleware, registry = proxy_mw

    async def call_next(ctx):
        raise ToolError("boom")

    with pytest.raises(ToolError):
        await middleware.on_call_tool(_ctx("government_report"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="government_report", transport="http", region="local",
                  status="error", error_category="unknown",
                  source="tool") == 1.0


async def test_proxied_is_error_result_sourced_upstream(proxy_mw):
    """The P3 dual path must carry the source label too."""
    middleware, registry = proxy_mw

    async def call_next(ctx):
        return SimpleNamespace(is_error=True)

    await middleware.on_call_tool(_ctx("dd_company_search"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="dd_company_search", transport="http", region="local",
                  status="error", error_category="unknown",
                  source="upstream") == 1.0


async def test_local_schema_rejection_on_proxied_tool_is_middleware(proxy_mw):
    """If THIS server rejects the arguments, the source is the middleware even
    though the tool is proxied — it never reached the remote."""
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    middleware, registry = proxy_mw

    async def call_next(ctx):
        raise FastMCPValidationError("bad args")

    with pytest.raises(FastMCPValidationError):
        await middleware.on_call_tool(_ctx("gov_govuk_search"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="gov_govuk_search", transport="http", region="local",
                  status="error", error_category="validation",
                  source="middleware") == 1.0


async def test_no_proxy_namespaces_means_nothing_is_upstream():
    """Default configuration: a server with no proxy mounts never emits
    source='upstream', however its tools happen to be named."""
    registry = CollectorRegistry()
    metrics = make_metrics("testfleet", registry=registry)
    middleware = FleetMetricsMiddleware(metrics)

    async def call_next(ctx):
        raise ToolError("boom")

    with pytest.raises(ToolError):
        await middleware.on_call_tool(_ctx("gov_govuk_search"), call_next)
    assert _value(registry, "testfleet_tool_calls_total",
                  tool="gov_govuk_search", transport="http", region="local",
                  status="error", error_category="unknown",
                  source="tool") == 1.0


async def test_integration_proxy_boundary_error_is_upstream_not_middleware():
    """The case that motivated the source label, end to end.

    An upstream server rejects the caller's arguments. Its pydantic
    ValidationError is serialised crossing the proxy boundary and arrives here
    as a ToolError, titled with the remote's UNPREFIXED tool name
    ('call[search]' while this server calls it 'gov_search'). So neither the
    exception class nor its title can identify it — but the mount
    configuration can, and does.
    """
    from fastmcp import Client, FastMCP
    from fastmcp.server import create_proxy

    upstream = FastMCP(name="upstream")

    @upstream.tool()
    def search(query: str) -> str:
        return "ok"

    registry = CollectorRegistry()
    metrics = make_metrics("integp", registry=registry)
    gateway = FastMCP(name="gateway")
    gateway.mount(create_proxy(upstream), namespace="gov")
    gateway.add_middleware(FleetMetricsMiddleware(metrics, proxy_namespaces=("gov",)))

    async with Client(gateway) as c:
        await c.call_tool_mcp("gov_search", {})  # missing required 'query'

    upstream_errors = [
        s for m in registry.collect() for s in m.samples
        if s.name == "integp_tool_calls_total"
        and s.labels.get("status") == "error"
    ]
    assert upstream_errors, "the failed proxied call was not counted at all"
    assert all(s.labels["source"] == "upstream" for s in upstream_errors), (
        f"expected every error sourced upstream, got "
        f"{[(s.labels['tool'], s.labels['source']) for s in upstream_errors]}")
