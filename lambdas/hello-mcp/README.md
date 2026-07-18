# hello-mcp Lambda

MCP server template that Claude.ai (or any MCP client) calls as a custom connector. Implements `initialize`, `tools/list`, and `tools/call` plus the `/.well-known/oauth-authorization-server` discovery document. See `docs/design-notes.md` for the architecture this is built from.

## Tools

- `hello_world(name)` — returns `Hello, {name}!` (defaults to `"world"`). Stands in for a real tool; proves the whole connector chain (discovery → OAuth login → token → tool call → usage counter) works before you invest in real logic.

## Adding a real tool

1. Add a module under `src/tools/` with `NAME`, `DESCRIPTION`, `INPUT_SCHEMA`, and a `call(arguments, context)` function — same shape as `tools/hello_world.py`.
2. Register it in `src/tools/__init__.py`'s `TOOLS` dict.
3. If the tool calls a slow upstream API, raise `aws_lambda_function.mcp_server`'s `timeout` in `terraform/mcp_server.tf` (capped at 30s — API Gateway's HTTP API integration timeout ceiling) and check `context.get_remaining_time_in_millis()` before starting work that might not finish in time.
4. If the tool needs a secret (API key, etc.), read it from SSM Parameter Store (SecureString) rather than baking it into an environment variable, and scope the Lambda role to `ssm:GetParameter` on that one parameter path.

## Environment Variables

| Variable | Required | Description |
|----------|----------|--------------|
| `USAGE_TABLE_NAME` | Yes | DynamoDB table name for the Layer 3 cumulative usage counter |
| `DAILY_LIMIT` | Yes | Max `tools/call` invocations per UTC day before short-circuiting |
| `MONTHLY_LIMIT` | Yes | Max `tools/call` invocations per UTC month before short-circuiting |
| `HOSTED_UI_DOMAIN` | Yes | Cognito Hosted UI base URL (e.g. `https://serverless-mcp.auth.us-east-1.amazoncognito.com`), used to build the discovery document's `authorization_endpoint`/`token_endpoint` |
| `COGNITO_ISSUER` | Yes | Cognito user pool issuer URL (`https://cognito-idp.<region>.amazonaws.com/<user_pool_id>`), used as the discovery document's `issuer` |

## Dependencies

None beyond boto3, which is provided by the Lambda runtime.

## Building

No `requirements.txt` — Terraform zips `src/` directly via `archive_file`.

## Payload Format

API Gateway HTTP API v2 proxy event. Routed by `rawPath`:

- `GET /.well-known/oauth-authorization-server` — returns the static OAuth Authorization Server Metadata JSON (RFC 8414), no auth required.
- `ANY /mcp` — behind the API Gateway JWT authorizer. Body is a single JSON-RPC 2.0 request; response is a single JSON-RPC 2.0 response (no SSE, no session tracking — each request is self-contained). `tools/call` params take the standard MCP shape: `{"name": "hello_world", "arguments": {"name": "Claude"}}`.

Before any `tools/call` does real work, it increments and checks the DynamoDB usage counter (Layer 3 in `docs/design-notes.md`'s rate-limiting design); if either the daily or monthly limit is exceeded, it returns an MCP tool error (`isError: true`, text `"usage cap reached"`) instead of proceeding. That log line is the intended target for a CloudWatch metric filter / alarm — not wired up here, add one if you deploy this for real.
