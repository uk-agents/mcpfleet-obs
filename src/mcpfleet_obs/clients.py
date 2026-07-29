"""Client tracking middleware."""
import logging

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .metrics import REGION, TRANSPORT, FleetMetrics


class ClientTrackingMiddleware(Middleware):
    """Track MCP client connections and increment counter on initialize.

    Logs client identity (name, version) and transport context on every initialize handshake.
    """

    def __init__(self, metrics: FleetMetrics, *, logger_name: str):
        self.metrics = metrics
        self._log = logging.getLogger(logger_name)

    async def on_request(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if context.method == "initialize":
            params = context.message.params
            info = getattr(params, "clientInfo", None)
            client_name = getattr(info, "name", "unknown") or "unknown"
            client_version = getattr(info, "version", "unknown") or "unknown"
            self._log.info("client_connected client=%s version=%s transport=%s region=%s",
                          client_name, client_version, TRANSPORT, REGION)
            self.metrics.client_connections_total.labels(client_name, client_version, TRANSPORT, REGION).inc()
        return result
