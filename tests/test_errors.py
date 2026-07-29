import json
import pathlib

import httpx
import pytest
from fastmcp.exceptions import ToolError

from mcpfleet_obs.errors import (
    FleetErrorPayload, parse_error_payload, raise_http_tool_error, raise_tool_error,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_raise_tool_error_payload_roundtrip():
    with pytest.raises(ToolError) as ei:
        raise_tool_error("transient", is_retryable=True,
                         attempted="t(x=1)", description="Upstream 503, retry.")
    payload = parse_error_payload(str(ei.value))
    assert payload is not None
    assert payload.error_category == "transient"
    assert payload.is_retryable is True
    assert payload.attempted == "t(x=1)"


def test_parse_error_payload_rejects_prose():
    assert parse_error_payload("Error calling tool 'x'") is None
    assert parse_error_payload('{"other": "json"}') is None


@pytest.mark.parametrize("status,category,retryable", [
    (404, "not_found", False),
    (403, "auth_required", False),
    (429, "transient", True),
    (503, "transient", True),
    (500, "unknown", False),
    (400, "validation", False),
    (422, "validation", False),
])
def test_http_status_mapping(status, category, retryable):
    req = httpx.Request("GET", "https://api.example/x")
    exc = httpx.HTTPStatusError("boom", request=req,
                                response=httpx.Response(status, request=req))
    with pytest.raises(ToolError) as ei:
        raise_http_tool_error(exc, attempted="t()")
    payload = parse_error_payload(str(ei.value))
    assert payload.error_category == category
    assert payload.is_retryable is retryable


def test_timeout_and_connect_are_transient():
    for exc in (httpx.TimeoutException("t"), httpx.ConnectError("c")):
        with pytest.raises(ToolError) as ei:
            raise_http_tool_error(exc, attempted="t()")
        assert parse_error_payload(str(ei.value)).error_category == "transient"


def test_contract_fixture_payloads_parse():
    """The structured error text captured from every probed fastmcp version
    must parse as a FleetErrorPayload — this is the cross-version contract."""
    checked = 0
    for f in FIXTURES.glob("error-shapes-*.json"):
        data = json.loads(f.read_text())
        for case in data["cases"]:
            if case["tool"] != "structured_tool_error":
                continue
            text = case["wire_result"]["content"][0]["text"]
            assert parse_error_payload(text) is not None, f.name
            checked += 1
    assert checked >= 5  # five fastmcp versions probed
