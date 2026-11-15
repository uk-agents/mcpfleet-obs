# mcpfleet-obs

Shared error taxonomy, metrics middleware, and client tracking for the mcpfleet MCP servers. One call installs the whole stack:

```python
from mcpfleet_obs import install

install(mcp, prefix="uk_legal")
```

This registers `ClientTrackingMiddleware`, `FleetMetricsMiddleware`, and a `GET /metrics` route on the FastMCP instance.

## Metrics

All metric names are prefixed (`{prefix}_tool_calls_total`, etc.).

- **`{prefix}_tool_calls_total`** — labels `tool`, `transport`, `region`, `status` (`ok`/`error`), `error_category`, `source`.
  - `error_category`: `transient`, `validation`, `not_found`, `auth_required`, `configuration`, `unknown`. Empty on success.
  - `source`: `middleware` (schema rejection / unknown tool name), `tool` (failed inside this server's tool body), `upstream` (failed in a `create_proxy` mount — see `proxy_namespaces`).
  - Calls to unregistered tool names fold into `tool="_unknown_"` (`not_found`/`middleware`) so client-supplied names can't mint series.
  - Both FastMCP error paths are counted: raised exceptions and result objects with `is_error=True`.
- **`{prefix}_tool_duration_seconds`** — histogram, labels `tool`, `transport`, `region`. The `tool` label uses the same `_unknown_` fold as the counter.
- **`{prefix}_client_connections_total`** — labels `client_name`, `client_version`, `transport`, `region`. Counted on both `initialize` and `server/discover` handshakes; reads `client_info` and `clientInfo` (MCP SDK 1.x/2.x). Distinct client names are bounded: the first `client_label_max` keep their own label, the rest fold to `"other"` (versions fold too). Default 40, override via the `CLIENT_LABEL_MAX` env var or `install(..., client_label_max=N)`.
- **`{prefix}_build_info`** — gauge, labels `server`, `obs_version`, `fastmcp_version`, `python_version`. Always 1.

## install() signature

```python
install(mcp, *, prefix, proxy_namespaces=(), client_label_max=CLIENT_LABEL_MAX, registry=None)
```

- `proxy_namespaces`: mount namespaces that forward via `create_proxy()` (e.g. `("gov", "law")`), so their tool errors are labelled `source="upstream"`.
- `registry`: defaults to the global `prometheus_client.REGISTRY`; pass a `CollectorRegistry` for isolated tests.

## Error taxonomy

Tools produce structured errors with `raise_tool_error` / `raise_http_tool_error` (from `mcpfleet_obs`), which raise `ToolError` carrying a `FleetErrorPayload` JSON body (`error_category`, `is_retryable`, `attempted`, `description`). The middleware parses the payload back out for the `error_category` label; consumers emitting the same JSON shape are classified identically. See `probes/fastmcp-error-shapes/PROBE-REPORT.md` in the mcpfleet workspace for the wire-contract research.
