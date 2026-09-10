"""Fleet-standard Prometheus metrics.

Dual-path error counting is REQUIRED, not defensive: on fastmcp 3.4.x a
proxied tool failure surfaces as a result with is_error=True and no
exception (probes/fastmcp-error-shapes/PROBE-REPORT.md, P3). Exception-only
middleware silently counts those as ok.
"""
import os
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from pydantic import ValidationError as PydanticValidationError

from .errors import parse_error_payload

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
TRANSPORT = os.getenv("FASTMCP_TRANSPORT", "http")
REGION = os.getenv("FLY_REGION", "local")


# Where an error was raised, derived from mount configuration rather than from
# the shape of the exception. Three causes, three owners, three fixes:
#   middleware -> this middleware rejected the call before the body ran
#   tool       -> domain code raised it (raise_tool_error)
#   upstream   -> a proxied mount; the remote server rejected it
# Empty string on success, matching how error_category is left empty there.
SOURCE_MIDDLEWARE = "middleware"
SOURCE_TOOL = "tool"
SOURCE_UPSTREAM = "upstream"


def _dist_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:  # editable checkout or vendored install
        return "unknown"


@dataclass
class FleetMetrics:
    tool_calls_total: Counter
    tool_duration_seconds: Histogram
    client_connections_total: Counter
    build_info: Gauge


def make_metrics(prefix: str, *, registry: CollectorRegistry | None = None) -> FleetMetrics:
    registry = registry if registry is not None else REGISTRY
    return FleetMetrics(
        tool_calls_total=Counter(
            f"{prefix}_tool_calls_total", "Count of MCP tool invocations.",
            labelnames=["tool", "transport", "region", "status", "error_category", "source"],
            registry=registry),
        tool_duration_seconds=Histogram(
            f"{prefix}_tool_duration_seconds", "Tool invocation latency in seconds.",
            labelnames=["tool", "transport", "region"], buckets=BUCKETS,
            registry=registry),
        client_connections_total=Counter(
            f"{prefix}_client_connections_total", "Count of MCP client initialize handshakes.",
            labelnames=["client_name", "client_version", "transport", "region"],
            registry=registry),
        # Deliberately NOT prefixed: one metric name across the fleet, with the
        # server told apart by label, so a single query answers "what is every
        # server actually running". The versions here are the ones that had to
        # be discovered by shelling into a container.
        build_info=Gauge(
            "mcpfleet_obs_build_info",
            "Build metadata for the running server. Always 1.",
            labelnames=["server", "obs_version", "fastmcp_version", "python_version"],
            registry=registry),
    )


def _is_argument_validation_error(exc: BaseException, tool: str) -> bool:
    """True when FastMCP rejected the CALLER's arguments against the tool
    signature, rather than a validation failure raised inside the tool body.

    The class moved inside the supported range: 3.2.x raises pydantic's
    ValidationError, 3.3+ raises FastMCP's own. Pydantic's is ambiguous — a
    tool body constructing a bad model raises the same class — so it counts
    as an argument error only when pydantic titled it for this call.
    Verified against 3.2.4 (title 'call[<tool>]') and 3.4.5 (FastMCP class).
    """
    if isinstance(exc, FastMCPValidationError):
        return True
    return (isinstance(exc, PydanticValidationError)
            and exc.title == f"call[{tool}]")


class FleetMetricsMiddleware(Middleware):
    """Counts tool calls, attributing each error to where it was raised.

    `proxy_namespaces` are the mount namespaces that forward to a remote
    server, e.g. ("gov", "law") for a server built with
    `mcp.mount(create_proxy(...), namespace="gov")`. They are taken as
    configuration rather than discovered, because discovery would mean walking
    `_WrappedProvider._transforms[]._prefix` and `_inner` — three levels of
    private attribute that differ across the supported fastmcp range, and the
    inner provider is a FastMCPProvider whether or not it proxies. A declared
    namespace is stable across pydantic versions, fastmcp versions and
    namespace renames; title-matching the exception is none of those.
    """

    def __init__(self, metrics: FleetMetrics,
                 proxy_namespaces: Sequence[str] = ()):
        self.metrics = metrics
        # Mounting with namespace="gov" prefixes its tools "gov_".
        self._proxy_prefixes = tuple(f"{ns}_" for ns in proxy_namespaces)

    def _source_of(self, tool: str) -> str:
        """Where a failure in this tool came from, by mount configuration."""
        return (SOURCE_UPSTREAM if tool.startswith(self._proxy_prefixes)
                else SOURCE_TOOL)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = context.message.name
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except NotFoundError:
            # Scanners probing bogus tool names must not mint metric series.
            self.metrics.tool_calls_total.labels(
                "_unknown_", TRANSPORT, REGION, "error", "not_found",
                SOURCE_MIDDLEWARE).inc()
            raise
        except (FastMCPValidationError, PydanticValidationError) as exc:
            # A schema rejection is the caller's input mistake, not an upstream
            # fault. Left in "unknown" it drowns out genuine faults on the
            # dashboard, since it is by far the most common error in practice.
            #
            # Reaching here at all means THIS server rejected the arguments, so
            # the source is the middleware even for a proxied tool. A proxied
            # call rejected at the far end arrives as a ToolError instead: the
            # remote's ValidationError is serialised on the way back, and it is
            # titled with the remote's unprefixed tool name. That is why the
            # source label cannot be inferred from the exception.
            # A body-raised pydantic error lands in this same branch, and it is
            # the tool's fault, not the middleware's — so the discriminator
            # decides the source as well as the category.
            is_argument_error = _is_argument_validation_error(exc, tool)
            category = "validation" if is_argument_error else "unknown"
            source = SOURCE_MIDDLEWARE if is_argument_error else self._source_of(tool)
            self.metrics.tool_calls_total.labels(
                tool, TRANSPORT, REGION, "error", category, source).inc()
            raise
        except ToolError as exc:
            payload = parse_error_payload(str(exc))
            category = payload.error_category if payload else "unknown"
            self.metrics.tool_calls_total.labels(
                tool, TRANSPORT, REGION, "error", category, self._source_of(tool)).inc()
            raise
        except BaseException:
            self.metrics.tool_calls_total.labels(
                tool, TRANSPORT, REGION, "error", "unknown", self._source_of(tool)).inc()
            raise
        finally:
            self.metrics.tool_duration_seconds.labels(tool, TRANSPORT, REGION).observe(
                time.perf_counter() - t0)
        if getattr(result, "is_error", False):
            payload = None
            content = getattr(result, "content", None)
            if content and getattr(content[0], "text", None):
                payload = parse_error_payload(content[0].text)
            category = payload.error_category if payload else "unknown"
            self.metrics.tool_calls_total.labels(
                tool, TRANSPORT, REGION, "error", category, self._source_of(tool)).inc()
        else:
            self.metrics.tool_calls_total.labels(
                tool, TRANSPORT, REGION, "ok", "", "").inc()
        return result
