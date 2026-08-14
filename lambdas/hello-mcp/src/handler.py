"""
Serverless MCP server template.

Implements the MCP JSON-RPC methods (`initialize`, `tools/list`, `tools/call`)
directly over API Gateway's request/response model, plus the OAuth discovery
document Claude.ai's connector setup fetches before login. No `mcp` SDK
dependency: that SDK's Streamable HTTP transport is built for a long-lived
ASGI/stdio server, not a single-shot Lambda invocation, and there's no ready
adapter between the two. Each POST /mcp request gets exactly one JSON
response (the stateless mode the Streamable HTTP spec allows) — no SSE, no
session tracking.

Payload format: API Gateway HTTP API v2 proxy event. Routes dispatched on
`rawPath`. Each MCP tool lives in its own module under `tools/`, registered
in `tools.TOOLS` — this file only owns the protocol/auth/rate-limit plumbing.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from tools import TOOLS
from usage_cap import usage_cap_reached

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HOSTED_UI_DOMAIN = os.environ["HOSTED_UI_DOMAIN"]
COGNITO_ISSUER = os.environ["COGNITO_ISSUER"]
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
API_BASE_URL = os.environ["API_BASE_URL"]
# Off by default — the OAuth proxy routes always log redacted bodies, but
# full headers are noisy and rarely needed once a flow is known-working.
VERBOSE_OAUTH_LOGGING = os.environ.get("VERBOSE_OAUTH_LOGGING", "").lower() == "true"

MCP_PROTOCOL_VERSION = "2025-06-18"


def _decode_body(event) -> str:
    # API Gateway HTTP API base64-encodes the body for some requests (e.g.
    # Postman's form-urlencoded token exchange); isBase64Encoded says which.
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return body


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------
def _jsonrpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# MCP JSON-RPC methods
# ---------------------------------------------------------------------------
def _handle_initialize(req_id, params, context):
    client_version = (params or {}).get("protocolVersion", MCP_PROTOCOL_VERSION)
    return _jsonrpc_result(
        req_id,
        {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "serverless-mcp-server", "version": "0.1.0"},
        },
    )


def _handle_tools_list(req_id, params, context):
    return _jsonrpc_result(
        req_id,
        {
            "tools": [
                {"name": mod.NAME, "description": mod.DESCRIPTION, "inputSchema": mod.INPUT_SCHEMA}
                for mod in TOOLS.values()
            ]
        },
    )


def _handle_tools_call(req_id, params, context):
    name = (params or {}).get("name")
    tool = TOOLS.get(name)

    if tool is None:
        logger.error("tools/call unknown tool | id=%s name=%s", req_id, name)
        return _jsonrpc_result(
            req_id, {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        )

    if usage_cap_reached():
        logger.warning("tools/call short-circuited by usage cap | id=%s name=%s", req_id, name)
        return _jsonrpc_result(req_id, {"content": [{"type": "text", "text": "usage cap reached"}], "isError": True})

    arguments = (params or {}).get("arguments") or {}
    result = tool.call(arguments, context)
    return _jsonrpc_result(req_id, result)


METHODS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


def _handle_mcp_request(body: str, context):
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError as e:
        logger.error("Failed to parse JSON-RPC body | error=%s", e)
        return _json_response(400, _jsonrpc_error(None, -32700, "Parse error"))

    # A JSON-RPC message with no "id" key is a notification — per spec it
    # must never receive a response, even an error one.
    is_notification = "id" not in payload
    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params")

    handler_fn = METHODS.get(method)
    if handler_fn is None:
        if is_notification:
            logger.info("Ignoring notification for unhandled method | method=%s", method)
            return _empty_response()
        logger.error("Unknown MCP method | method=%s", method)
        return _json_response(200, _jsonrpc_error(req_id, -32601, "Method not found"))

    logger.info("Handling MCP method | method=%s id=%s params=%s", method, req_id, params)
    result = handler_fn(req_id, params, context)
    logger.info("MCP response | method=%s id=%s result=%s", method, req_id, json.dumps(result))
    return _json_response(200, result)


# ---------------------------------------------------------------------------
# OAuth discovery document (RFC 8414) — see docs/design-notes.md §3.2
# ---------------------------------------------------------------------------
def _handle_discovery():
    logger.info("Serving OAuth discovery document")
    document = {
        # Deliberately our own API base, not Cognito's real issuer: a
        # spec-compliant client resolves `authorization_servers` (protected
        # resource metadata, below) straight into an RFC 8414 discovery
        # fetch, bypassing this document entirely unless the two match. Real
        # Cognito access tokens still carry Cognito's own `iss` claim
        # (AWS won't let that be overridden) — a client that cross-checks
        # token `iss` against this `issuer` will reject the token. Traded
        # off in favor of CloudWatch visibility into the OAuth flow; see
        # docs/chatgpt-oauth-notes.md.
        "issuer": API_BASE_URL,
        # Points at our own proxy route (below), not Cognito directly, so the
        # authorization request's query params — notably `scope` — pass through
        # our Lambda and land in CloudWatch before being forwarded to Cognito.
        "authorization_endpoint": f"{API_BASE_URL}/oauth2/authorize",
        "token_endpoint": f"{API_BASE_URL}/oauth2/token",
        "jwks_uri": f"{API_BASE_URL}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ["openid", "email"],
    }
    logger.info("Discovery document served | document=%s", json.dumps(document))
    return _json_response(200, document)


# ---------------------------------------------------------------------------
# OAuth protected resource metadata (RFC 9728) — tells MCP clients which
# authorization server(s) protect this resource. Required by the MCP
# Authorization spec independently of the oauth-authorization-server
# document above; clients fetch this first, before ever redirecting to login.
# ---------------------------------------------------------------------------
def _handle_protected_resource_metadata():
    logger.info("Serving OAuth protected resource metadata")
    document = {
        "resource": MCP_SERVER_URL,
        # Our own base, not COGNITO_ISSUER — see the comment on "issuer" in
        # _handle_discovery() above for why.
        "authorization_servers": [API_BASE_URL],
        "scopes_supported": ["openid", "email"],
    }
    logger.info("Protected resource metadata served | document=%s", json.dumps(document))
    return _json_response(200, document)


# ---------------------------------------------------------------------------
# Authorization endpoint proxy — debugging/interception shim.
#
# ChatGPT's connector redirects the browser straight to Cognito's
# /oauth2/authorize, so its query params never reach our logs — leaving us
# blind to what `scope` it actually requests when Cognito rejects it with
# invalid_scope. Advertising this route as the authorization_endpoint routes
# that request through the Lambda first: we log the exact query string, then
# 302 on to Cognito's real authorize endpoint, forwarding the params verbatim
# (minus whatever STRIP_OAUTH_PARAMS configures — see _DROPPED_OAUTH_PARAMS
# below, empty/standards-compliant by default).
# ---------------------------------------------------------------------------
# Nothing is dropped by default — Cognito has a resource server registered
# (mcp_cognito.tf) and accepts standard params, including RFC 8707's
# `resource`, natively via "resource binding". Set STRIP_OAUTH_PARAMS to a
# comma-separated list of param names (e.g. "resource" or "resource,foo") to
# drop specific params again before forwarding to Cognito, e.g. if a
# differently-configured pool rejects something a client sends. See
# docs/chatgpt-oauth-notes.md.
def _parse_param_list(value: str) -> tuple:
    return tuple(name.strip() for name in value.split(",") if name.strip())


_DROPPED_OAUTH_PARAMS = _parse_param_list(os.environ.get("STRIP_OAUTH_PARAMS", ""))

_REDACTED = "***REDACTED***"
_REDACTED_HEADERS = ("authorization",)


def _headers_for_log(headers: dict) -> str:
    if not VERBOSE_OAUTH_LOGGING:
        return "<omitted, set VERBOSE_OAUTH_LOGGING=true to log headers>"
    redacted = {k: (_REDACTED if k.lower() in _REDACTED_HEADERS else v) for k, v in headers.items()}
    return json.dumps(redacted)


def _drop_params(form_encoded: str, names) -> str:
    # Rebuilt by dropping whole `name=value` pairs rather than round-tripping
    # through parse_qs/urlencode, so untouched pairs keep their original
    # encoding byte-for-byte (parse_qs/urlencode would e.g. turn %20 into +).
    if not form_encoded:
        return form_encoded
    names = set(names)
    kept = [
        pair
        for pair in form_encoded.split("&")
        if urllib.parse.unquote_plus(pair.split("=", 1)[0]) not in names
    ]
    return "&".join(kept)


def _handle_authorize_proxy(event):
    raw_query = event.get("rawQueryString", "")
    logger.info(
        "Authorize proxy | inbound from API Gateway | method=%s headers=%s raw_query=%s params=%s",
        event.get("requestContext", {}).get("http", {}).get("method"),
        _headers_for_log(event.get("headers") or {}),
        raw_query,
        json.dumps(event.get("queryStringParameters") or {}),
    )
    forward_query = _drop_params(raw_query, _DROPPED_OAUTH_PARAMS) if raw_query else raw_query
    target = f"{HOSTED_UI_DOMAIN}/oauth2/authorize"
    if forward_query:
        target = f"{target}?{forward_query}"
    logger.info("Authorize proxy | outbound to API Gateway | redirect=%s", target)
    return _redirect_response(target)


# ---------------------------------------------------------------------------
# Token endpoint proxy — same visibility goal as the authorize proxy above:
# the token exchange (and refresh) would otherwise go straight from the
# client to Cognito, invisible to our CloudWatch logs. Advertised as
# token_endpoint in the discovery document. Unlike the GET-only authorize
# proxy, this route carries real credentials, so request/response secrets
# (client_secret, code, code_verifier, access/id/refresh tokens) are
# redacted before logging.
# ---------------------------------------------------------------------------
_REQUEST_SECRET_FIELDS = ("client_secret", "code", "code_verifier", "refresh_token")
_RESPONSE_SECRET_FIELDS = ("access_token", "id_token", "refresh_token")


def _redact_form_body(body: str) -> str:
    params = urllib.parse.parse_qs(body, keep_blank_values=True)
    for field in _REQUEST_SECRET_FIELDS:
        if field in params:
            params[field] = [_REDACTED]
    return urllib.parse.urlencode(params, doseq=True)


def _redact_token_response(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    for field in _RESPONSE_SECRET_FIELDS:
        if field in data:
            data[field] = _REDACTED
    return json.dumps(data)


def _proxy_to_cognito(url: str, method: str, body: bytes | None, headers: dict):
    req = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "application/json"), dict(e.headers)
    except OSError as e:
        # Covers urllib.error.URLError (DNS/connection failures) and
        # (Timeout)Error (also an OSError) from the timeout above — neither
        # is an HTTPError, so without this Cognito being briefly unreachable
        # would surface as an opaque 500 instead of a diagnosable response.
        logger.error("Proxy request to Cognito failed | url=%s error=%s", url, e)
        error_body = json.dumps({"error": "upstream_unavailable", "error_description": str(e)}).encode()
        return 502, error_body, "application/json", {}


def _log_token_hop(label: str, headers: dict, redacted_body: str, **fields):
    prefix = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info(
        "Token proxy | %s | %s headers=%s body=%s",
        label,
        prefix,
        _headers_for_log(headers),
        redacted_body,
    )


def _handle_token_proxy(event):
    body = _decode_body(event)
    inbound_headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    _log_token_hop(
        "inbound from API Gateway",
        inbound_headers,
        _redact_form_body(body),
        method=event.get("requestContext", {}).get("http", {}).get("method"),
    )

    forward_body = _drop_params(body, _DROPPED_OAUTH_PARAMS)
    forward_headers = {"Content-Type": inbound_headers.get("content-type", "application/x-www-form-urlencoded")}
    if "authorization" in inbound_headers:
        forward_headers["Authorization"] = inbound_headers["authorization"]

    token_url = f"{HOSTED_UI_DOMAIN}/oauth2/token"
    _log_token_hop("outbound to Cognito", forward_headers, _redact_form_body(forward_body), url=token_url)

    status, resp_body, content_type, resp_headers = _proxy_to_cognito(
        token_url, "POST", forward_body.encode(), forward_headers
    )
    resp_text = resp_body.decode()
    _log_token_hop("inbound from Cognito", resp_headers, _redact_token_response(resp_text), status=status)

    response = {"statusCode": status, "headers": {"Content-Type": content_type}, "body": resp_text}
    _log_token_hop(
        "outbound to API Gateway",
        response["headers"],
        _redact_token_response(response["body"]),
        statusCode=response["statusCode"],
    )
    return response


# ---------------------------------------------------------------------------
# JWKS proxy — public keys, nothing sensitive to redact. Advertised as
# jwks_uri in the discovery document so this fetch also shows up in our logs.
# ---------------------------------------------------------------------------
def _handle_jwks_proxy():
    url = f"{COGNITO_ISSUER}/.well-known/jwks.json"
    logger.info("JWKS proxy request | url=%s", url)
    status, resp_body, content_type, _ = _proxy_to_cognito(url, "GET", None, {})
    logger.info("JWKS proxy response | status=%s", status)
    return {"statusCode": status, "headers": {"Content-Type": content_type}, "body": resp_body.decode()}


def _redirect_response(location: str):
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _json_response(status_code: int, body: dict):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _empty_response():
    # For JSON-RPC notifications, which never get a response body.
    return {"statusCode": 202, "headers": {}, "body": ""}


def _caller_identity(event) -> str:
    # Populated by the API Gateway JWT authorizer on /mcp; absent on the
    # unauthenticated discovery route.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return claims.get("email") or claims.get("username") or claims.get("sub") or "unknown"


def handler(event, context):
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    identity = _caller_identity(event)
    request_id = context.aws_request_id if context else "unknown"
    logger.info(
        "Request received | request_id=%s method=%s path=%s identity=%s source_ip=%s",
        request_id,
        method,
        path,
        identity,
        event.get("requestContext", {}).get("http", {}).get("sourceIp"),
    )

    try:
        if path == "/.well-known/oauth-authorization-server":
            return _handle_discovery()
        if path == "/.well-known/oauth-protected-resource":
            return _handle_protected_resource_metadata()
        if path == "/oauth2/authorize":
            return _handle_authorize_proxy(event)
        if path == "/oauth2/token":
            return _handle_token_proxy(event)
        if path == "/.well-known/jwks.json":
            return _handle_jwks_proxy()
        if path == "/mcp":
            if method != "POST":
                logger.error("Unsupported method on /mcp | method=%s", method)
                return _json_response(405, {"error": "method not allowed"})
            return _handle_mcp_request(_decode_body(event), context)

        logger.error("Unknown route | path=%s", path)
        return _json_response(404, {"error": "not found"})
    except Exception:
        logger.exception("Unhandled error | request_id=%s path=%s", request_id, path)
        return _json_response(500, {"error": "internal server error"})
