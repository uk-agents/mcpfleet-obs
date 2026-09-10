"""One-call installation of observability middleware stack."""
import platform
from collections.abc import Sequence

from fastmcp import FastMCP
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from .clients import ClientTrackingMiddleware
from .metrics import FleetMetrics, FleetMetricsMiddleware, _dist_version, make_metrics


def install(mcp: FastMCP, *, prefix: str,
            proxy_namespaces: Sequence[str] = (),
            registry: CollectorRegistry | None = None) -> FleetMetrics:
    """Install the fleet observability stack: client tracking, metrics, and /metrics endpoint.

    Args:
        mcp: FastMCP instance to instrument
        prefix: Prometheus metric name prefix (e.g. "uk_legal")
        proxy_namespaces: Mount namespaces that forward to a remote server, e.g.
            ("gov", "law") for `mcp.mount(create_proxy(...), namespace="gov")`.
            Errors from tools under these namespaces are labelled
            source="upstream" rather than source="tool", because the failure
            belongs to the remote server and not to this one. Leave empty for a
            server with no proxy mounts.
        registry: Optional Prometheus registry (uses default if None)

    Returns:
        FleetMetrics instance (for further configuration if needed)
    """
    reg = registry if registry is not None else REGISTRY
    metrics = make_metrics(prefix, registry=reg)

    # Set once, at registration. Answers "what is this server actually running"
    # without shelling into the container to find out.
    metrics.build_info.labels(
        server=prefix,
        obs_version=_dist_version("mcpfleet-obs"),
        fastmcp_version=_dist_version("fastmcp"),
        python_version=platform.python_version(),
    ).set(1)

    mcp.add_middleware(ClientTrackingMiddleware(metrics, logger_name=f"fastmcp.{prefix}.clients"))
    mcp.add_middleware(FleetMetricsMiddleware(metrics, proxy_namespaces=proxy_namespaces))

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics_endpoint(request: Request) -> Response:
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    return metrics
