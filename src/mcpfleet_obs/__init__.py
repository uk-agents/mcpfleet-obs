"""Fleet-wide observability for MCP servers: error taxonomy, metrics middleware, client tracking."""

from .clients import ClientTrackingMiddleware
from .errors import FleetErrorPayload, parse_error_payload, raise_http_tool_error, raise_tool_error
from .install import install
from .metrics import FleetMetricsMiddleware, make_metrics

__all__ = [
    "install",
    "make_metrics",
    "FleetMetricsMiddleware",
    "ClientTrackingMiddleware",
    "raise_tool_error",
    "raise_http_tool_error",
    "parse_error_payload",
    "FleetErrorPayload",
]
