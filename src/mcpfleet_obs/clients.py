"""Client tracking middleware."""
import logging
import os

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .metrics import REGION, TRANSPORT, FleetMetrics

CLIENT_LABEL_MAX = int(os.getenv("CLIENT_LABEL_MAX", "40"))


class _BoundedLabel:
    """First-come bound on how many distinct values a metric label can take.

    Client names are chosen by whoever connects, and every distinct value is a
    permanent series. The first `limit` distinct values keep their own label;
    later ones fold into "other". The exact name is still in the
    client_connected log line.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._seen: set[str] = set()

    def resolve(self, value: str) -> str:
        if value in self._seen:
            return value
        if len(self._seen) < self._limit:
            self._seen.add(value)
            return value
        return "other"


class ClientTrackingMiddleware(Middleware):
    """Track MCP client connections and increment counter on initialize.

    Logs client identity (name, version) and transport context on every initialize handshake.
    """

    def __init__(self, metrics: FleetMetrics, *, logger_name: str,
                 client_label_max: int = CLIENT_LABEL_MAX):
        self.metrics = metrics
        self._log = logging.getLogger(logger_name)
        self._client_labels = _BoundedLabel(client_label_max)

    async def on_request(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if context.method in _HANDSHAKE_METHODS:
            client_name, client_version = _client_identity(context)
            caps = _field(context.message.params, "capabilities")
            cap_keys = [k for k, v in (caps.model_dump().items() if hasattr(caps, "model_dump") else []) if v is not None]
            self._log.info("client_connected client=%s version=%s capabilities=%s transport=%s region=%s",
                          client_name, client_version, cap_keys, TRANSPORT, REGION)
            label = self._client_labels.resolve(client_name)
            # Version folds with the name so a folded client can't mint one
            # series per version string.
            self.metrics.client_connections_total.labels(
                label, client_version if label != "other" else "other", TRANSPORT, REGION
            ).inc()
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
        # The MCP SDK renamed InitializeRequestParams.clientInfo to client_info in 2.x
        # (FastMCP 4). Reading only the old name reported every handshake-era client
        # as "unknown" there; the old name is still what SDK 1.x (FastMCP 3) exposes.
        info = _field(params, "client_info") or _field(params, "clientInfo")
    else:
        info = _field(_field(params, "meta") or {}, _META_CLIENT_INFO)
    return (_field(info, "name") or "unknown", _field(info, "version") or "unknown")
