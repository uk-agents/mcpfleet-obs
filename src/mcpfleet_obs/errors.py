"""Fleet error taxonomy. Payload contract verified against fastmcp 3.2.3-4.0.0b1
(probes/fastmcp-error-shapes/PROBE-REPORT.md): ToolError messages survive
mask_error_details=True and create_proxy() hops verbatim."""
from typing import Literal, NoReturn

import httpx
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

ErrorCategory = Literal[
    "transient", "validation", "not_found", "auth_required", "configuration", "unknown"
]


class FleetErrorPayload(BaseModel):
    error_category: ErrorCategory
    is_retryable: bool
    attempted: str
    description: str


def raise_tool_error(category: ErrorCategory, *, is_retryable: bool,
                     attempted: str, description: str) -> NoReturn:
    raise ToolError(FleetErrorPayload(
        error_category=category, is_retryable=is_retryable,
        attempted=attempted, description=description,
    ).model_dump_json())


def raise_http_tool_error(exc: Exception, *, attempted: str) -> NoReturn:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        url = exc.request.url
        if status == 404:
            raise_tool_error("not_found", is_retryable=False, attempted=attempted,
                             description=f"Resource not found (404). Check the identifier. URL: {url}")
        if status in (401, 403):
            raise_tool_error("auth_required", is_retryable=False, attempted=attempted,
                             description=f"Access denied ({status}). URL: {url}")
        if status in (429, 503):
            raise_tool_error("transient", is_retryable=True, attempted=attempted,
                             description=f"Upstream returned {status} — retry after a short delay. URL: {url}")
        if 400 <= status < 500:
            raise_tool_error("validation", is_retryable=False, attempted=attempted,
                             description=f"Upstream rejected the request ({status}) — fix the input and retry. URL: {url}")
        raise_tool_error("unknown", is_retryable=False, attempted=attempted,
                         description=f"Upstream returned {status}. URL: {url}")
    if isinstance(exc, httpx.TimeoutException):
        raise_tool_error("transient", is_retryable=True, attempted=attempted,
                         description="Request timed out — upstream may be slow, retry is safe.")
    if isinstance(exc, httpx.ConnectError):
        raise_tool_error("transient", is_retryable=True, attempted=attempted,
                         description="Could not connect to upstream API — network error, retry is safe.")
    raise_tool_error("unknown", is_retryable=False, attempted=attempted,
                     description=f"Unexpected error: {type(exc).__name__}: {exc}")


def parse_error_payload(message: str) -> FleetErrorPayload | None:
    try:
        return FleetErrorPayload.model_validate_json(message)
    except Exception:
        return None
