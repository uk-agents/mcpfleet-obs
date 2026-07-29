"""Fleet-standard Prometheus metrics.

Dual-path error counting is REQUIRED, not defensive: on fastmcp 3.4.x a
proxied tool failure surfaces as a result with is_error=True and no
exception (probes/fastmcp-error-shapes/PROBE-REPORT.md, P3). Exception-only
middleware silently counts those as ok.
"""
import os
import time
from dataclasses import dataclass

from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

from .errors import parse_error_payload

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
TRANSPORT = os.getenv("FASTMCP_TRANSPORT", "http")
REGION = os.getenv("FLY_REGION", "local")


@dataclass
class FleetMetrics:
    tool_calls_total: Counter
    tool_duration_seconds: Histogram
    client_connections_total: Counter


def make_metrics(prefix: str, *, registry: CollectorRegistry | None = None) -> FleetMetrics:
    registry = registry if registry is not None else REGISTRY
    return FleetMetrics(
        tool_calls_total=Counter(
            f"{prefix}_tool_calls_total", "Count of MCP tool invocations.",
            labelnames=["tool", "transport", "region", "status", "error_category"],
            registry=registry),
        tool_duration_seconds=Histogram(
            f"{prefix}_tool_duration_seconds", "Tool invocation latency in seconds.",
            labelnames=["tool", "transport", "region"], buckets=BUCKETS,
            registry=registry),
        client_connections_total=Counter(
            f"{prefix}_client_connections_total", "Count of MCP client initialize handshakes.",
            labelnames=["client_name", "client_version", "transport", "region"],
            registry=registry),
    )


class FleetMetricsMiddleware(Middleware):
    def __init__(self, metrics: FleetMetrics):
        self.metrics = metrics

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = context.message.name
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except NotFoundError:
            # Scanners probing bogus tool names must not mint metric series.
            self.metrics.tool_calls_total.labels(
                "_unknown_", TRANSPORT, REGION, "error", "not_found").inc()
            raise
        except ToolError as exc:
            payload = parse_error_payload(str(exc))
            category = payload.error_category if payload else "unknown"
            self.metrics.tool_calls_total.labels(tool, TRANSPORT, REGION, "error", category).inc()
            raise
        except BaseException:
            self.metrics.tool_calls_total.labels(tool, TRANSPORT, REGION, "error", "unknown").inc()
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
            self.metrics.tool_calls_total.labels(tool, TRANSPORT, REGION, "error", category).inc()
        else:
            self.metrics.tool_calls_total.labels(tool, TRANSPORT, REGION, "ok", "").inc()
        return result
