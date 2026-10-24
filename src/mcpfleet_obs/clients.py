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
        if context.method in _HANDSHAKE_METHODS:
            client_name, client_version = _client_identity(context)
            self._log.info("client_connected client=%s version=%s transport=%s region=%s",
                          client_name, client_version, TRANSPORT, REGION)
            self.metrics.client_connections_total.labels(client_name, client_version, TRANSPORT, REGION).inc()
        return result


# Handshake-era clients open with "initialize". The 2026-07-28 protocol is sessionless
# and opens with "server/discover" instead, which carries clientInfo in params._meta.
# Counting only "initialize" made FastMCP 4 modern clients invisible to the metric.
_HANDSHAKE_METHODS = ("initialize", "server/discover")
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"


def _field(obj, key: str):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _client_identity(context: MiddlewareContext) -> tuple[str, str]:
    params = context.message.params
    if context.method == "initialize":
        info = _field(params, "clientInfo")
    else:
        info = _field(_field(params, "meta") or {}, _META_CLIENT_INFO)
    return (_field(info, "name") or "unknown", _field(info, "version") or "unknown")
