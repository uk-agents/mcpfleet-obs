"""Client identity: handshake tracking plus per-call resolution.

This module is deliberately the leaf of the package's import graph — it must
not import .metrics at runtime, because .metrics imports the resolvers here.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING

from fastmcp.server.middleware import Middleware, MiddlewareContext

try:
    from fastmcp.server.dependencies import get_http_request
except ImportError:  # fastmcp outside the tested matrix
    def get_http_request():
        raise RuntimeError("get_http_request unavailable")

if TYPE_CHECKING:
    from .metrics import FleetMetrics

TRANSPORT = os.getenv("FASTMCP_TRANSPORT", "http")
REGION = os.getenv("FLY_REGION", "local")

CLIENT_LABEL_MAX = int(os.getenv("CLIENT_LABEL_MAX", "40"))

# Per-boot salt: hashes distinguish IPs within a process lifetime without
# letting a label value track a caller across restarts or leak the raw IP.
IP_LABEL_ENABLED = os.getenv("MCPOBS_IP_LABEL", "").lower() in ("1", "true", "yes")
_IP_SALT = os.urandom(16)


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
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


def _field(obj, key: str):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def resolve_call_client(context: MiddlewareContext, labels: _BoundedLabel) -> str:
    """Who made this tools/call, for the tool_calls_total `client` label.

    The fleet runs stateless_http=True: every request is a fresh connection,
    so session.client_params is never populated for a tools/call and identity
    must come from what the request itself carries:

      1. request _meta clientInfo — SDK v2 / 2026-07-28 clients stamp it on
         EVERY request. The transport lifts raw _meta onto
         request_context.meta across the supported matrix (verified on
         3.2.4 and 3.4.5); context.message.meta covers direct/in-process
         calls where the wire was never involved.
      2. session.client_params — stateful transports and stdio.
      3. HTTP User-Agent — the only signal a legacy client leaves on each
         request; labelled "ua:<token>" so the population stays
         distinguishable from handshake-declared names.
      4. "unknown".
    """
    ctx = getattr(context, "fastmcp_context", None)
    rc = getattr(ctx, "request_context", None)

    for meta in (_field(rc, "meta"), _field(context.message, "meta")):
        if meta is not None and not isinstance(meta, dict):
            # SDK 1.x pydantic Meta model: extra keys land in model_extra.
            meta = getattr(meta, "model_extra", None)
        name = _field(_field(meta, CLIENT_INFO_META_KEY), "name")
        if name:
            return labels.resolve(name)

    session = getattr(rc, "session", None)
    client_params = getattr(session, "client_params", None)
    if client_params is not None:
        info = _field(client_params, "client_info") or _field(client_params, "clientInfo")
        name = _field(info, "name")
        if name:
            return labels.resolve(name)

    try:
        ua = get_http_request().headers.get("user-agent") or ""
    except Exception:
        ua = ""
    return labels.resolve(f"ua:{ua.split()[0]}") if ua else "unknown"


def resolve_ip_hash() -> str:
    """Per-boot pseudonym of the caller's IP, or "" when MCPOBS_IP_LABEL is off.

    Header precedence: cf-connecting-ip (authoritative when Cloudflare-fronted),
    then fly-client-ip (authoritative on Fly — the edge overwrites it), then
    the first x-forwarded-for hop (fallback for other deployments). A caller
    can spoof xff, but only to mint noise, not to impersonate another client.
    """
    if not IP_LABEL_ENABLED:
        return ""
    try:
        headers = get_http_request().headers
    except Exception:
        return ""
    xff = (headers.get("x-forwarded-for") or "").split(",")
    ip = (headers.get("cf-connecting-ip")
          or headers.get("fly-client-ip")
          or (xff[0].strip() if xff else ""))
    return hashlib.sha256(_IP_SALT + ip.encode()).hexdigest()[:12] if ip else ""


def _client_identity(context: MiddlewareContext) -> tuple[str, str]:
    params = context.message.params
    if context.method == "initialize":
        # The MCP SDK renamed InitializeRequestParams.clientInfo to client_info in 2.x
        # (FastMCP 4). Reading only the old name reported every handshake-era client
        # as "unknown" there; the old name is still what SDK 1.x (FastMCP 3) exposes.
        info = _field(params, "client_info") or _field(params, "clientInfo")
    else:
        info = _field(_field(params, "meta") or {}, CLIENT_INFO_META_KEY)
    return (_field(info, "name") or "unknown", _field(info, "version") or "unknown")
