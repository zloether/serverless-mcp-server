# ChatGPT custom connector — OAuth support notes

How ChatGPT's MCP connector does OAuth discovery and login, and the specific
things this template does to accommodate it (beyond what a spec-minimal
Cognito setup would need). See `docs/design-notes.md` for the general
architecture and `docs/DEBUGGING.md` for how to test this flow by hand.

## How ChatGPT's connector negotiates OAuth

In order, before a user ever sees a login screen:

1. **`GET /.well-known/oauth-protected-resource`** (RFC 9728) — ChatGPT
   fetches this first, before opening any authorization tab. It lists which
   authorization server(s) protect this resource. Missing this endpoint
   causes ChatGPT's auth tab to open and close immediately with no visible
   error.
2. **RFC 8414 discovery against whatever `authorization_servers` names** —
   ChatGPT takes that value verbatim and performs its own
   `/.well-known/openid-configuration`-style fetch directly against it. If it
   names Cognito's real issuer, ChatGPT resolves Cognito's own metadata and
   never touches this server's endpoints again for the rest of the flow.
3. **`GET /oauth2/authorize`** — the query string it sends here (`scope`,
   PKCE params, `resource`, etc.) is derived from what the discovery
   documents advertised in steps 1–2.
4. **`POST /oauth2/token`** — the authorization code + PKCE verifier exchange,
   with the same `resource` param repeated from step 3.
5. **`GET /.well-known/jwks.json`** — to validate the returned token.

## Why this server proxies every one of those endpoints

Cognito could serve steps 3–5 directly — nothing requires an intermediary.
This template proxies all of them through the Lambda instead
(`_handle_authorize_proxy`, `_handle_token_proxy`, `_handle_jwks_proxy` in
`lambdas/hello-mcp/src/handler.py`), for two reasons:

- **Visibility.** A client's browser/server talking straight to Cognito
  never touches this server's CloudWatch logs — there'd be no way to see
  what a misbehaving client actually sent, or what Cognito actually rejected
  and why. Routing through the Lambda means every hop of the OAuth flow is
  logged (redacted — see `docs/DEBUGGING.md` §4), independent of any
  client's own error reporting.
- **Discovery document control.** Both this server's own
  `/.well-known/oauth-authorization-server` document and the RFC 9728
  `authorization_servers` field deliberately point `issuer` at this API's own
  base URL (`API_BASE_URL`), not Cognito's real issuer
  (`COGNITO_ISSUER`) — otherwise a spec-compliant client's RFC 8414 discovery
  (step 2 above) resolves straight to Cognito's native, unproxied endpoints,
  defeating the point of proxying them at all.

  This has one accepted tradeoff: Cognito access tokens always carry
  Cognito's real `iss` claim (AWS provides no way to override it), so a
  client that cross-checks a token's `iss` against the discovery document's
  `issuer` could see a mismatch. ChatGPT's connector does not appear to
  perform this check in practice.

## The `resource` param (RFC 8707)

ChatGPT's connector sends an OAuth `resource` parameter — this MCP server's
own URL — on both the authorize and token requests. Cognito rejects the PKCE
token exchange outright when `resource` is present and no resource server is
registered for it (`invalid_grant`), even though the same request without it
succeeds.

Fixed properly rather than worked around: `mcp_cognito.tf` registers an
`aws_cognito_resource_server` whose `identifier` matches this server's own
URL (`MCP_SERVER_URL`, what ChatGPT sends as `resource`). This turns on
Cognito's native **"resource binding"** (Managed Login only), which does two
things — accepts `resource=<identifier>` at `/oauth2/authorize` instead of
rejecting the token exchange, and sets the issued access token's `aud` claim
to that identifier. Both proxy routes forward `resource` to Cognito
unmodified by default; nothing is dropped. `handler.py`'s
`_DROPPED_OAUTH_PARAMS` (populated from `var.mcp_strip_oauth_params`, empty
by default) is a generic escape hatch — a comma-separated list of any param
names to strip before forwarding to Cognito, for a pool without a matching
resource server or a client sending something else Cognito rejects.

## Known limitations

- **No `WWW-Authenticate` header on 401s.** API Gateway HTTP API's native
  JWT authorizer can't emit custom response headers. Fixing this would
  require swapping to a Lambda authorizer — a bigger, more invasive change
  to auth-critical code.
