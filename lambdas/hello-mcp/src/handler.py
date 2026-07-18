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

import json
import logging
import os

from tools import TOOLS
from usage_cap import usage_cap_reached

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HOSTED_UI_DOMAIN = os.environ["HOSTED_UI_DOMAIN"]
COGNITO_ISSUER = os.environ["COGNITO_ISSUER"]

MCP_PROTOCOL_VERSION = "2025-06-18"


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
        "issuer": COGNITO_ISSUER,
        "authorization_endpoint": f"{HOSTED_UI_DOMAIN}/oauth2/authorize",
        "token_endpoint": f"{HOSTED_UI_DOMAIN}/oauth2/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ["openid", "email"],
    }
    logger.info("Discovery document served | document=%s", json.dumps(document))
    return _json_response(200, document)


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
        if path == "/mcp":
            if method != "POST":
                logger.error("Unsupported method on /mcp | method=%s", method)
                return _json_response(405, {"error": "method not allowed"})
            return _handle_mcp_request(event.get("body", ""), context)

        logger.error("Unknown route | path=%s", path)
        return _json_response(404, {"error": "not found"})
    except Exception:
        logger.exception("Unhandled error | request_id=%s path=%s", request_id, path)
        return _json_response(500, {"error": "internal server error"})
