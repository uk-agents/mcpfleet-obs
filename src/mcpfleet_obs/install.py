"""One-call installation of observability middleware stack."""
from fastmcp import FastMCP
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from .clients import ClientTrackingMiddleware
from .metrics import FleetMetrics, FleetMetricsMiddleware, make_metrics


def install(mcp: FastMCP, *, prefix: str,
            registry: CollectorRegistry | None = None) -> FleetMetrics:
    """Install the fleet observability stack: client tracking, metrics, and /metrics endpoint.

    Args:
        mcp: FastMCP instance to instrument
        prefix: Prometheus metric name prefix (e.g. "uk_legal")
        registry: Optional Prometheus registry (uses default if None)

    Returns:
        FleetMetrics instance (for further configuration if needed)
    """
    reg = registry if registry is not None else REGISTRY
    metrics = make_metrics(prefix, registry=reg)
    mcp.add_middleware(ClientTrackingMiddleware(metrics, logger_name=f"fastmcp.{prefix}.clients"))
    mcp.add_middleware(FleetMetricsMiddleware(metrics))

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics_endpoint(request: Request) -> Response:
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    return metrics
