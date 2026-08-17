# Debugging — testing the OAuth + MCP chain with Postman

When a connector (Claude.ai, ChatGPT, etc.) fails to connect, it's hard to
tell whether the problem is this server or the client's OAuth implementation
— the client's own browser popup often swallows the real error. Postman lets
you drive the same OAuth flow by hand and see exactly what Cognito and the
Lambda return at each step, independent of any client quirks.

## 1. Allow Postman's callback URL

Postman's OAuth 2.0 helper redirects to `https://oauth.pstmn.io/v1/callback`.
Claude.ai's and Claude.com's callback URLs are always allowed
(`locals.mcp_default_oauth_callback_urls` in `mcp_cognito.tf`), so
`mcp_oauth_callback_urls` only needs Postman's added on top. Add this to
`terraform/dev.auto.tfvars` (gitignored — see `AGENTS.md`'s Conventions
section), then `terraform apply`:

```hcl
mcp_oauth_callback_urls = [
  "https://oauth.pstmn.io/v1/callback",
]
```

## 2. Get a token via Postman's OAuth 2.0 helper

Every OAuth endpoint the client touches — `/oauth2/authorize`,
`/oauth2/token`, and `/.well-known/jwks.json` — is proxied through this
server's own Lambda rather than hit on Cognito's domain directly (see
`_handle_authorize_proxy` / `_handle_token_proxy` / `_handle_jwks_proxy` in
`handler.py`). Pointing Postman at the proxy routes below means the whole
flow shows up in CloudWatch, not just the parts that happened to route
through the Lambda already.

In a Postman request, go to the **Authorization** tab → type **OAuth 2.0** →
**Get New Access Token**, and fill in:

| Field | Value |
|---|---|
| Grant Type | Authorization Code |
| Callback URL | `https://oauth.pstmn.io/v1/callback` |
| Auth URL | `<mcp_server_url base>/oauth2/authorize` |
| Access Token URL | `<mcp_server_url base>/oauth2/token` |
| Client ID | `terraform output -raw mcp_cognito_app_client_id` |
| Client Secret | `terraform output -raw mcp_cognito_app_client_secret` |
| Scope | `openid email` |
| Client Authentication | Send as Basic Auth header |

`<mcp_server_url base>` is `terraform output -raw mcp_server_url` with the
trailing `/mcp` stripped (e.g. `https://abc123.execute-api.us-east-1.amazonaws.com`).

**Check "Authorize using browser."** Postman's embedded webview doesn't
render Cognito's Managed Login UI, so the login page appears blank/broken
unless you force it to open in your system browser instead.

Both `client_secret_basic` and `client_secret_post` work against the Cognito
app client, so either Client Authentication option is fine.

Click **Get New Access Token** and log in — Postman captures the access
token, and CloudWatch Logs for the Lambda shows both the authorize request
and the token exchange (secrets/tokens redacted — see §4).

## 3. Call the MCP server

With the token attached (Postman does this automatically after step 2, or
add `Authorization: Bearer <token>` manually), `POST` to the MCP server URL
(`terraform output -raw mcp_server_url`) with a JSON-RPC body:

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
```

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
```

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hello_world", "arguments": {}}}
```

A 200 response with a JSON-RPC `result` confirms auth, routing, and the tool
call all work — the same chain a real connector exercises end to end.

## 4. Inspect what a client actually sends

By default, a client's browser would hit Cognito's `/oauth2/authorize` and
`/oauth2/token` directly, and its server-side code would fetch
`/.well-known/jwks.json` directly — none of that touches this Lambda, so
CloudWatch has no visibility into what was actually sent (e.g. what `scope`
a misbehaving client requested, or why a token exchange failed). The
discovery document advertises all three as this server's own proxy routes
instead (`_handle_authorize_proxy`, `_handle_token_proxy`,
`_handle_jwks_proxy` in `handler.py`) specifically to work around this: each
logs the request, then forwards it to Cognito's real endpoint and returns
the response verbatim.

- `Authorize proxy | inbound from API Gateway | ...` — method, headers, and
  the full authorization request query string, including `scope`.
- `Authorize proxy | outbound to API Gateway | redirect=...` — the exact
  Cognito URL the browser gets 302'd to, **after** any params configured via
  `STRIP_OAUTH_PARAMS` (see below) are dropped — none by default. There's no
  separate "Cognito's response" log for this route — the browser talks to
  Cognito directly after the redirect, so the only server-side visibility is
  what we redirected it to.
- `Token proxy | inbound from API Gateway | ...` — method, headers, and the
  token exchange body as received, with `client_secret`, `code`, and
  `code_verifier` redacted.
- `Token proxy | outbound to Cognito | ...` — URL, headers, and body actually
  sent to Cognito, same redaction, **after** any configured params are
  dropped.
- `Token proxy | inbound from Cognito | ...` — Cognito's raw status, headers,
  and body, with `access_token` / `id_token` / `refresh_token` redacted
  (only `token_type`/`expires_in`/etc. are logged in the clear).
- `Token proxy | outbound to API Gateway | ...` — the final status/headers/
  body this Lambda hands back, same redaction.

As long as Postman (or the real client) uses the proxy routes from §2 rather
than Cognito's domain directly, this shows up automatically — no separate
step needed.

**Headers are omitted by default.** Every line above always logs the
(redacted) body, but the `headers=...` field just shows
`<omitted, set VERBOSE_OAUTH_LOGGING=true to log headers>` unless the
Lambda's `VERBOSE_OAUTH_LOGGING` env var is `"true"` — full headers are
rarely needed once a flow is known-working, and they're the noisiest part of
these log lines. Set it via `-var mcp_verbose_oauth_logging=true` on
`terraform apply` (or a `.tfvars` file) when you actually need to see them, e.g. to check a `redirect_uri` scheme/host mismatch that
only shows up in a header, or a client sending an unexpected
`Content-Type`. Even with it on, the `Authorization` header itself is still
redacted (`_headers_for_log` in `handler.py`) — it carries the
`client_secret_basic` credential, and turning on verbose logging shouldn't
mean writing that to CloudWatch in plaintext.

**`resource` (RFC 8707) is forwarded, not dropped.** This user pool has a
resource server registered (`mcp_cognito.tf`), so Cognito accepts it natively
via resource binding — see `docs/chatgpt-oauth-notes.md`. Nothing is stripped
by default; `var.mcp_strip_oauth_params` (empty by default) can name specific
params for both proxy routes to drop before talking to Cognito, e.g. for a
pool without a matching resource server. If you've set it and want to see
what a client actually sent before a param was dropped, look at the
`inbound from API Gateway` log line, not `outbound to Cognito`.

## Common failure points

| Symptom | Likely cause |
|---|---|
| Postman's auth popup is blank or won't render | Forgot to check "Authorize using browser" |
| `redirect_uri_mismatch` from Cognito | Callback URL not in `mcp_oauth_callback_urls` — re-apply after adding it |
| `invalid_scope` from Cognito's `/oauth2/authorize` | Requested scope isn't in Cognito's `allowed_oauth_scopes` (`mcp_cognito.tf`) — check via the authorize proxy (§4) to see what was actually requested |
| `invalid_grant` at token exchange | Usually a stale/reused authorization code — restart the flow from "Get New Access Token". If the code/verifier/redirect_uri all check out via the logs, also check whether the client is sending a `resource` param (RFC 8707) — Cognito rejects it unless a matching `aws_cognito_resource_server` is registered (`mcp_cognito.tf` already registers one for this server's own URL); a client sending some other param Cognito doesn't expect can hit the same failure mode, fixable by adding it to `var.mcp_strip_oauth_params`. See `docs/chatgpt-oauth-notes.md` |
| `invalid_request` at token exchange, and `Token proxy request \| body=...` in CloudWatch looks like an unredacted blob instead of a redacted form string | API Gateway base64-encoded the body and it wasn't decoded before forwarding to Cognito — fixed by `_decode_body()` in `handler.py`; make sure the deployed Lambda is current (re-run `terraform apply`) |
| 401 from `/mcp` with a valid-looking token | Token's `client_id` claim doesn't match the API Gateway JWT authorizer's configured `audience` — see `docs/design-notes.md` §3.1 |
| `invalid_request` from a manually-built `curl` token exchange, even though every field looks right | `curl -d` does **not** URL-encode values — it sends `redirect_uri`/`resource` with a literal `://` in the body. Use `--data-urlencode "field=value"` per field instead, which matches what real clients and Postman send |
| `invalid_grant`/`invalid_request` from a manual PKCE `curl` reproduction that a real client doesn't hit | Double check `code_verifier` is the raw verifier, not the challenge — a base64url string containing `-`/`_` and ~43 characters is the *challenge* (SHA-256 hash); the verifier is the longer, unhashed value used to generate it |

## See also

- [`docs/chatgpt-oauth-notes.md`](chatgpt-oauth-notes.md) — how ChatGPT's
  connector specifically does OAuth discovery and login, and what this
  template does to support it.
- [`docs/design-notes.md`](design-notes.md) §3.1 — why Cognito access tokens
  need the `audience`-as-`client_id` workaround in the first place.
