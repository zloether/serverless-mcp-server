import base64
import importlib.util
import json
import os
import sys
import time
import urllib.error
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


def test_initialize_rejects_unsupported_protocol_version_with_server_default():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-01-01"}}
    )
    assert _body(result)["result"]["protocolVersion"] == mcp_server.MCP_PROTOCOL_VERSION


def test_initialize_echoes_supported_protocol_version():
    result = _call_mcp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": mcp_server.MCP_PROTOCOL_VERSION},
        }
    )
    assert _body(result)["result"]["protocolVersion"] == mcp_server.MCP_PROTOCOL_VERSION


def test_initialize_with_string_params_returns_invalid_params():
    result = _call_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "str"})
    body = _body(result)
    assert body["error"]["code"] == -32602


def test_initialize_with_list_protocol_version_returns_server_default():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": [1, 2]}}
    )
    assert _body(result)["result"]["protocolVersion"] == mcp_server.MCP_PROTOCOL_VERSION


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


def test_usage_counter_increment_sets_expires_at_ttl():
    table = _usage_table_mock()
    with patch.object(usage_cap, "usage_table", table):
        usage_cap._increment_and_check("date#2026-01-01", 200)
    kwargs = table.update_item.call_args.kwargs
    assert "SET expires_at" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":expires_at"] > time.time()


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


def test_notification_for_known_method_gets_no_body():
    table = _usage_table_mock()
    with patch.object(usage_cap, "usage_table", table):
        result = mcp_server.handler(
            _mcp_event({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "hello_world"}}), None
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


def test_batch_payload_returns_invalid_request():
    event = _mcp_event(None)
    event["body"] = "[]"
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == -32600


def test_string_payload_returns_invalid_request():
    event = _mcp_event(None)
    event["body"] = '"hi"'
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == -32600


def test_number_payload_returns_invalid_request():
    event = _mcp_event(None)
    event["body"] = "5"
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == -32600


def test_null_payload_returns_invalid_request():
    event = _mcp_event(None)
    event["body"] = "null"
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == -32600


def test_tools_call_with_dict_name_returns_invalid_params():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": {"a": 1}}}
    )
    body = _body(result)
    assert body["error"]["code"] == -32602


def test_tools_call_with_list_arguments_returns_invalid_params():
    result = _call_mcp(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "hello_world", "arguments": [1, 2]},
        }
    )
    body = _body(result)
    assert body["error"]["code"] == -32602


def test_tools_call_with_string_params_returns_invalid_params():
    result = _call_mcp({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": "str"})
    body = _body(result)
    assert body["error"]["code"] == -32602


def test_monthly_cap_reached_blocks_hello_world_while_daily_ok():
    result = _call_mcp(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "hello_world"}},
        daily_count=1,
        monthly_count=5001,
    )
    body = _body(result)["result"]
    assert body["isError"] is True
    assert body["content"][0]["text"] == "usage cap reached"


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
    # issuer is our own base, not Cognito's — so RFC 8414 discovery off
    # authorization_servers (protected resource metadata) lands here rather
    # than on Cognito's native, unproxied metadata document.
    assert doc["issuer"] == os.environ["API_BASE_URL"]
    # authorization_endpoint points at our own proxy route, not Cognito.
    assert doc["authorization_endpoint"] == f"{os.environ['API_BASE_URL']}/oauth2/authorize"
    # token_endpoint and jwks_uri also point at our own proxy routes, not
    # Cognito directly — see the token/jwks proxy tests below.
    assert doc["token_endpoint"] == f"{os.environ['API_BASE_URL']}/oauth2/token"
    assert doc["jwks_uri"] == f"{os.environ['API_BASE_URL']}/.well-known/jwks.json"


# ---------------------------------------------------------------------------
# protected resource metadata
# ---------------------------------------------------------------------------

def test_protected_resource_metadata_contains_resource_and_authorization_server():
    event = {
        "rawPath": "/.well-known/oauth-protected-resource",
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 200
    doc = _body(result)
    assert doc["resource"] == os.environ["MCP_SERVER_URL"]
    assert doc["authorization_servers"] == [os.environ["API_BASE_URL"]]
    assert doc["scopes_supported"] == ["openid", "email"]


# ---------------------------------------------------------------------------
# authorize proxy
# ---------------------------------------------------------------------------

def test_authorize_proxy_redirects_to_cognito_preserving_query():
    raw_query = "response_type=code&client_id=abc&scope=openid+email&state=xyz"
    event = {
        "rawPath": "/oauth2/authorize",
        "rawQueryString": raw_query,
        "queryStringParameters": {"scope": "openid email"},
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 302
    assert result["headers"]["Location"] == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/authorize?{raw_query}"


def test_authorize_proxy_forwards_resource_param_by_default():
    raw_query = "response_type=code&client_id=abc&resource=https%3A%2F%2Fexample.com%2Fmcp&state=xyz"
    event = {
        "rawPath": "/oauth2/authorize",
        "rawQueryString": raw_query,
        "queryStringParameters": {},
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    result = mcp_server.handler(event, None)
    assert result["headers"]["Location"] == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/authorize?{raw_query}"


def test_authorize_proxy_strips_params_when_configured():
    raw_query = "response_type=code&client_id=abc&resource=https%3A%2F%2Fexample.com%2Fmcp&state=xyz"
    event = {
        "rawPath": "/oauth2/authorize",
        "rawQueryString": raw_query,
        "queryStringParameters": {},
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    with patch.object(mcp_server, "_DROPPED_OAUTH_PARAMS", ("resource",)):
        result = mcp_server.handler(event, None)
    location = result["headers"]["Location"]
    assert "resource" not in location
    assert "response_type=code" in location
    assert "client_id=abc" in location
    assert "state=xyz" in location


def test_authorize_proxy_without_query_redirects_to_bare_endpoint():
    event = {
        "rawPath": "/oauth2/authorize",
        "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}},
    }
    result = mcp_server.handler(event, None)
    assert result["statusCode"] == 302
    assert result["headers"]["Location"] == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/authorize"


# ---------------------------------------------------------------------------
# token proxy
# ---------------------------------------------------------------------------

def _http_response(body: bytes, status=200, content_type="application/json"):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.headers = {"Content-Type": content_type}
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _token_event(body, headers=None):
    return {
        "rawPath": "/oauth2/token",
        "headers": headers or {"content-type": "application/x-www-form-urlencoded"},
        "body": body,
        "requestContext": {"http": {"method": "POST", "sourceIp": "1.2.3.4"}},
    }


def test_token_proxy_forwards_to_cognito_token_endpoint():
    resp = _http_response(b'{"access_token": "at", "token_type": "Bearer"}')
    event = _token_event("grant_type=authorization_code&client_id=abc&client_secret=shh&code=xyz")
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        result = mcp_server.handler(event, None)
    assert result["statusCode"] == 200
    assert _body(result) == {"access_token": "at", "token_type": "Bearer"}
    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.full_url == f"{os.environ['HOSTED_UI_DOMAIN']}/oauth2/token"


def test_token_proxy_forwards_authorization_header():
    resp = _http_response(b'{"access_token": "at"}')
    event = _token_event("grant_type=authorization_code", headers={"authorization": "Basic abc123"})
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        mcp_server.handler(event, None)
    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Authorization") == "Basic abc123"


def test_token_proxy_decodes_base64_body():
    resp = _http_response(b'{"access_token": "at"}')
    raw = "grant_type=authorization_code&client_id=abc&client_secret=shh&code=xyz"
    event = _token_event(base64.b64encode(raw.encode()).decode())
    event["isBase64Encoded"] = True
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        result = mcp_server.handler(event, None)
    assert result["statusCode"] == 200
    sent_body = mock_urlopen.call_args[0][0].data
    assert sent_body == raw.encode()


def test_token_proxy_forwards_cache_control_and_pragma_headers():
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b'{"access_token": "at"}'
    resp.headers = {"Content-Type": "application/json", "Cache-Control": "no-store", "Pragma": "no-cache"}
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    event = _token_event("grant_type=authorization_code&client_id=abc&client_secret=shh&code=xyz")
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp):
        result = mcp_server.handler(event, None)
    assert result["headers"]["Cache-Control"] == "no-store"
    assert result["headers"]["Pragma"] == "no-cache"


def test_token_proxy_surfaces_cognito_error_status():
    error = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
    error.read = MagicMock(return_value=b'{"error": "invalid_grant"}')
    error.headers = {"Content-Type": "application/json"}
    with patch("mcp_server_handler.urllib.request.urlopen", side_effect=error):
        result = mcp_server.handler(_token_event("grant_type=authorization_code"), None)
    assert result["statusCode"] == 400
    assert _body(result) == {"error": "invalid_grant"}


def test_token_proxy_forwards_resource_param_by_default():
    resp = _http_response(b'{"access_token": "at"}')
    event = _token_event(
        "grant_type=authorization_code&client_id=abc&client_secret=shh&code=xyz"
        "&resource=https%3A%2F%2Fexample.com%2Fmcp"
    )
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        mcp_server.handler(event, None)
    sent_body = mock_urlopen.call_args[0][0].data.decode()
    assert "resource=https%3A%2F%2Fexample.com%2Fmcp" in sent_body


def test_token_proxy_strips_params_when_configured():
    resp = _http_response(b'{"access_token": "at"}')
    event = _token_event(
        "grant_type=authorization_code&client_id=abc&client_secret=shh&code=xyz"
        "&resource=https%3A%2F%2Fexample.com%2Fmcp"
    )
    with patch.object(mcp_server, "_DROPPED_OAUTH_PARAMS", ("resource",)):
        with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
            mcp_server.handler(event, None)
    sent_body = mock_urlopen.call_args[0][0].data.decode()
    assert "resource" not in sent_body
    assert "grant_type=authorization_code" in sent_body
    assert "code=xyz" in sent_body


def test_parse_param_list_splits_and_trims_comma_separated_names():
    assert mcp_server._parse_param_list("resource, foo,,bar") == ("resource", "foo", "bar")
    assert mcp_server._parse_param_list("") == ()


def test_redact_form_body_masks_client_secret_and_code():
    redacted = mcp_server._redact_form_body(
        "client_id=abc&client_secret=shh&code=xyz&grant_type=authorization_code"
    )
    assert "shh" not in redacted
    assert "xyz" not in redacted
    assert "client_id=abc" in redacted
    assert "grant_type=authorization_code" in redacted


def test_redact_form_body_masks_refresh_token():
    redacted = mcp_server._redact_form_body("grant_type=refresh_token&refresh_token=super-secret&client_id=abc")
    assert "super-secret" not in redacted
    assert "client_id=abc" in redacted


def test_headers_for_log_omits_headers_by_default():
    assert "Basic abc123" not in mcp_server._headers_for_log({"authorization": "Basic abc123"})


def test_headers_for_log_redacts_authorization_when_verbose():
    with patch.object(mcp_server, "VERBOSE_OAUTH_LOGGING", True):
        logged = mcp_server._headers_for_log({"authorization": "Basic abc123", "content-type": "text/plain"})
    assert "abc123" not in logged
    assert "text/plain" in logged


def test_headers_for_log_redacts_cookie_and_set_cookie_when_verbose():
    with patch.object(mcp_server, "VERBOSE_OAUTH_LOGGING", True):
        logged = mcp_server._headers_for_log(
            {"cookie": "session=secret", "set-cookie": "session=secret2", "content-type": "text/plain"}
        )
    assert "secret" not in logged
    assert "secret2" not in logged
    assert "text/plain" in logged


def test_drop_params_preserves_encoding_of_untouched_params():
    result = mcp_server._drop_params("redirect_uri=https%3A%2F%2Fx.com%2Fcb%3Fa%3Db%20c&resource=foo", ("resource",))
    assert result == "redirect_uri=https%3A%2F%2Fx.com%2Fcb%3Fa%3Db%20c"


def test_proxy_to_cognito_returns_502_on_network_error():
    with patch("mcp_server_handler.urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        result = mcp_server.handler(_token_event("grant_type=authorization_code"), None)
    assert result["statusCode"] == 502
    assert _body(result)["error"] == "upstream_unavailable"


def test_redact_token_response_masks_tokens_only():
    redacted = json.loads(
        mcp_server._redact_token_response(json.dumps({"access_token": "at", "refresh_token": "rt", "token_type": "Bearer"}))
    )
    assert redacted["access_token"] == mcp_server._REDACTED
    assert redacted["refresh_token"] == mcp_server._REDACTED
    assert redacted["token_type"] == "Bearer"


# ---------------------------------------------------------------------------
# jwks proxy
# ---------------------------------------------------------------------------

def test_jwks_proxy_forwards_to_cognito():
    resp = _http_response(b'{"keys": []}')
    event = {"rawPath": "/.well-known/jwks.json", "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}}}
    with patch("mcp_server_handler.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        result = mcp_server.handler(event, None)
    assert result["statusCode"] == 200
    assert _body(result) == {"keys": []}
    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.full_url == f"{os.environ['COGNITO_ISSUER']}/.well-known/jwks.json"


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
