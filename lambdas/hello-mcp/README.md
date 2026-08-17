# hello-mcp Lambda

MCP server template that Claude.ai or ChatGPT (tested) — or in principle any MCP client that supports custom connectors (untested) — calls as a custom connector. Implements `initialize`, `tools/list`, and `tools/call` plus the `/.well-known/oauth-authorization-server` discovery document. See `docs/design-notes.md` for the architecture this is built from.

## Tools

- `hello_world(name)` — returns `Hello, {name}!` (defaults to `"world"`). Stands in for a real tool; proves the whole connector chain (discovery → OAuth login → token → tool call → usage counter) works before you invest in real logic.

## Adding a real tool

1. Add a module under `src/tools/` with `NAME`, `DESCRIPTION`, `INPUT_SCHEMA`, and a `call(arguments, context)` function — same shape as `tools/hello_world.py`. `INPUT_SCHEMA` is advertised to clients via `tools/list` but not enforced by `handler.py` — `arguments` reaches `call()` unvalidated, so validate it yourself if your tool needs more than `hello_world`'s no-args case.
2. Register it in `src/tools/__init__.py`'s `TOOLS` dict.
3. If the tool calls a slow upstream API, raise `aws_lambda_function.mcp_server`'s `timeout` in `terraform/mcp_server.tf` (capped at 30s — API Gateway's HTTP API integration timeout ceiling) and check `context.get_remaining_time_in_millis()` before starting work that might not finish in time.
4. If the tool needs a secret (API key, etc.), read it from SSM Parameter Store (SecureString) rather than baking it into an environment variable, and scope the Lambda role to `ssm:GetParameter` on that one parameter path.
5. `handler.py` logs every `tools/call`'s full `params` and `result` at INFO unconditionally (unlike the OAuth proxy routes, which redact by default). Fine for `hello_world`, but if your tool's arguments or return value carry PII or other sensitive data, redact those fields yourself before returning/logging — there's no generic gate for this since the template can't know in advance what's sensitive in a tool it doesn't define.

## Environment Variables

| Variable | Required | Description |
|----------|----------|--------------|
| `USAGE_TABLE_NAME` | Yes | DynamoDB table name for the Layer 3 cumulative usage counter |
| `DAILY_LIMIT` | Yes | Max `tools/call` invocations per UTC day before short-circuiting |
| `MONTHLY_LIMIT` | Yes | Max `tools/call` invocations per UTC month before short-circuiting |
| `HOSTED_UI_DOMAIN` | Yes | Cognito Hosted UI base URL (e.g. `https://serverless-mcp.auth.us-east-1.amazoncognito.com`) — the real endpoint the `/oauth2/authorize` and `/oauth2/token` proxy routes forward to |
| `COGNITO_ISSUER` | Yes | Cognito user pool issuer URL (`https://cognito-idp.<region>.amazonaws.com/<user_pool_id>`), used to build the `/.well-known/jwks.json` proxy target |
| `MCP_SERVER_URL` | Yes | This server's own `/mcp` URL, used as the `resource` field in the protected-resource metadata document (RFC 9728) |
| `API_BASE_URL` | Yes | This API Gateway's base URL, used as the discovery document's `issuer`/`authorization_servers` and to build this server's own OAuth proxy routes (`authorization_endpoint`, `token_endpoint`, `jwks_uri`) |
| `VERBOSE_OAUTH_LOGGING` | No | Set to `"true"` to have the OAuth proxy routes (`/oauth2/authorize`, `/oauth2/token`) log full request/response headers to CloudWatch. Off by default — the proxy routes always log redacted bodies regardless; this only controls the noisier header dumps. See `docs/DEBUGGING.md` |
| `STRIP_OAUTH_PARAMS` | No | Comma-separated list of query/body param names (e.g. `"resource"` or `"resource,foo"`) for the OAuth proxy routes to drop before forwarding to Cognito. Empty by default — Cognito has a resource server registered (`terraform/mcp_cognito.tf`) and accepts standard params, including RFC 8707's `resource`, natively. See `docs/chatgpt-oauth-notes.md` |

## Dependencies

None beyond boto3, which is provided by the Lambda runtime.

## Building

No `requirements.txt` — Terraform zips `src/` directly via `archive_file`.

## Payload Format

API Gateway HTTP API v2 proxy event. Routed by `rawPath`:

- `GET /.well-known/oauth-authorization-server` — returns the static OAuth Authorization Server Metadata JSON (RFC 8414), no auth required.
- `GET /.well-known/oauth-protected-resource` — returns the static OAuth Protected Resource Metadata JSON (RFC 9728), no auth required.
- `GET /oauth2/authorize` — proxies to Cognito's Hosted UI `/oauth2/authorize`. Logs the inbound query string, then 302s to Cognito with the query forwarded verbatim (minus whatever `STRIP_OAUTH_PARAMS` configures — nothing, by default; see `docs/chatgpt-oauth-notes.md`).
- `POST /oauth2/token` — proxies to Cognito's `/oauth2/token`. Logs the request/response at every hop (inbound from API Gateway, outbound to Cognito, inbound from Cognito, outbound to API Gateway) with `client_secret`/`code`/`code_verifier`/`access_token`/`id_token`/`refresh_token` redacted, and drops the same `STRIP_OAUTH_PARAMS`-configured params before forwarding, same as the authorize proxy.
- `GET /.well-known/jwks.json` — proxies to Cognito's JWKS endpoint, no auth required.
- `ANY /mcp` — behind the API Gateway JWT authorizer. Body is a single JSON-RPC 2.0 request; response is a single JSON-RPC 2.0 response (no SSE, no session tracking — each request is self-contained). `tools/call` params take the standard MCP shape: `{"name": "hello_world", "arguments": {"name": "Claude"}}`.

Before any `tools/call` does real work, it increments and checks the DynamoDB usage counter (Layer 3 in `docs/design-notes.md`'s rate-limiting design); if either the daily or monthly limit is exceeded, it returns an MCP tool error (`isError: true`, text `"usage cap reached"`) instead of proceeding. That log line is the intended target for a CloudWatch metric filter / alarm — not wired up here, add one if you deploy this for real.
