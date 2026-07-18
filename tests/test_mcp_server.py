import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

_PATH = os.path.join(os.path.dirname(__file__), "../lambdas/hello-mcp/src/handler.py")
_SRC_DIR = os.path.dirname(_PATH)
if _SRC_DIR not in sys.path:
    # handler.py does `from tools import TOOLS` / `from usage_cap import ...` —
    # absolute imports of sibling modules under src/, so src/ must be on
    # sys.path before exec_module below.
    sys.path.insert(0, _SRC_DIR)

_spec = importlib.util.spec_from_file_location("mcp_server_handler", _PATH)
mcp_server = importlib.util.module_from_spec(_spec)
sys.modules["mcp_server_handler"] = mcp_server
_spec.loader.exec_module(mcp_server)

# usage_cap is imported as a side effect of exec_module above (handler.py does
# `from usage_cap import usage_cap_reached`), so it's already in sys.modules.
usage_cap = sys.modules["usage_cap"]


def _usage_table_mock(daily_count=1, monthly_count=1):
    table = MagicMock()

    def update_item(**kwargs):
        counter_id = kwargs["Key"]["counter_id"]
        count = daily_count if counter_id.startswith("date#") else monthly_count
        return {"Attributes": {"count": count}}

    table.update_item.side_effect = update_item
    return table


def _mcp_event(payload, method="POST"):
    return {
        "rawPath": "/mcp",
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "body": json.dumps(payload) if payload is not None else None,
    }


def _call_mcp(payload, method="POST", daily_count=1, monthly_count=1):
    with patch.object(usage_cap, "usage_table", _usage_table_mock(daily_count, monthly_count)):
        return mcp_server.handler(_mcp_event(payload, method=method), None)


def _body(result):
    return json.loads(result["body"])


# ---------------------------------------------------------------------------
# initialize / tools/list
# ---------------------------------------------------------------------------

def test_initialize_returns_capabilities_and_server_info():
    result = _call_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert result["statusCode"] == 200
    body = _body(result)
    assert body["result"]["serverInfo"]["name"] == "serverless-mcp-server"
    assert body["result"]["capabilities"] == {"tools": {}}


def test_initialize_echoes_client_protocol_version():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-01-01"}}
    )
    assert _body(result)["result"]["protocolVersion"] == "2024-01-01"


def test_tools_list_returns_hello_world_tool():
    result = _call_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = _body(result)["result"]["tools"]
    assert [t["name"] for t in tools] == ["hello_world"]


# ---------------------------------------------------------------------------
# tools/call — hello_world, usage cap
# ---------------------------------------------------------------------------

def test_hello_world_call_defaults_to_world():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hello_world"}},
        daily_count=1,
        monthly_count=1,
    )
    body = _body(result)["result"]
    assert body["isError"] is False
    assert body["content"][0]["text"] == "Hello, world!"


def test_hello_world_call_greets_given_name():
    result = _call_mcp(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "hello_world", "arguments": {"name": "Claude"}},
        }
    )
    body = _body(result)["result"]
    assert body["content"][0]["text"] == "Hello, Claude!"


def test_hello_world_call_increments_both_usage_counters():
    table = _usage_table_mock(daily_count=1, monthly_count=1)
    with patch.object(usage_cap, "usage_table", table):
        mcp_server.handler(
            _mcp_event({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hello_world"}}), None
        )
    assert table.update_item.call_count == 2


def test_unknown_tool_returns_error_without_touching_usage_counters():
    table = _usage_table_mock()
    with patch.object(usage_cap, "usage_table", table):
        result = mcp_server.handler(
            _mcp_event({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bogus"}}), None
        )
    body = _body(result)["result"]
    assert body["isError"] is True
    assert "unknown tool" in body["content"][0]["text"]
    table.update_item.assert_not_called()


def test_usage_cap_reached_blocks_hello_world():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "hello_world"}},
        daily_count=201,
        monthly_count=1,
    )
    body = _body(result)["result"]
    assert body["isError"] is True
    assert body["content"][0]["text"] == "usage cap reached"


def test_daily_cap_reached_short_circuits_monthly_increment():
    table = _usage_table_mock(daily_count=201, monthly_count=1)
    with patch.object(usage_cap, "usage_table", table):
        mcp_server.handler(
            _mcp_event({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "hello_world"}}), None
        )
    # Only the daily counter should be written once the daily cap is already exceeded.
    assert table.update_item.call_count == 1


# ---------------------------------------------------------------------------
# notifications vs requests
# ---------------------------------------------------------------------------

def test_notification_for_unhandled_method_gets_no_body():
    table = _usage_table_mock()
    with patch.object(usage_cap, "usage_table", table):
        result = mcp_server.handler(
            _mcp_event({"jsonrpc": "2.0", "method": "notifications/initialized"}), None
        )
    assert result["statusCode"] == 202
    assert result["body"] == ""
    table.update_item.assert_not_called()


def test_request_with_id_for_unknown_method_returns_jsonrpc_error():
    result = _call_mcp({"jsonrpc": "2.0", "id": 7, "method": "bogus/method"})
    assert result["statusCode"] == 200
    body = _body(result)
    assert body["error"]["code"] == -32601
    assert body["id"] == 7


# ---------------------------------------------------------------------------
# transport-level edge cases
# ---------------------------------------------------------------------------

def test_malformed_json_returns_400():
    event = _mcp_event(None)
    event["body"] = "not-json"
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == -32700


def test_non_post_method_on_mcp_returns_405():
    result = _call_mcp({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, method="GET")
    assert result["statusCode"] == 405


def test_unknown_route_returns_404():
    event = {"rawPath": "/nope", "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}}}
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 404


def test_unhandled_exception_returns_500():
    event = {"rawPath": "/.well-known/oauth-authorization-server", "requestContext": {"http": {"method": "GET"}}}
    with patch.object(mcp_server, "_handle_discovery", side_effect=RuntimeError("boom")):
        result = mcp_server.handler(event, None)
    assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# discovery document
# ---------------------------------------------------------------------------

def test_discovery_document_contains_expected_endpoints():
    event = {
        "rawPath": "/.well-known/oauth-authorization-server",
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 200
    doc = _body(result)
    assert doc["issuer"] == os.environ["COGNITO_ISSUER"]
    assert doc["authorization_endpoint"] == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/authorize"
    assert doc["token_endpoint"] == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/token"


# ---------------------------------------------------------------------------
# caller identity
# ---------------------------------------------------------------------------

def test_caller_identity_prefers_email():
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"email": "a@b.com", "sub": "abc"}}}}}
    assert mcp_server._caller_identity(event) == "a@b.com"


def test_caller_identity_falls_back_to_username_then_sub():
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"username": "u1", "sub": "abc"}}}}}
    assert mcp_server._caller_identity(event) == "u1"

    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "abc"}}}}}
    assert mcp_server._caller_identity(event) == "abc"


def test_caller_identity_defaults_to_unknown():
    assert mcp_server._caller_identity({}) == "unknown"
