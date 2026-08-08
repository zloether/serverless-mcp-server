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
own URL — on both the authorize and token requests. This Cognito user pool
has no resource server registered for it, and Cognito rejects the PKCE token
exchange outright when `resource` is present (`invalid_grant`), even though
the same request without it succeeds.

Both proxy routes drop `resource` before forwarding to Cognito
(`_DROPPED_OAUTH_PARAMS` / `_drop_params` in `handler.py`) rather than
trying to get Cognito to honor it. This is not a client-specific special
case — the MCP client that provided the reproduction happened to be
ChatGPT, but this fix applies to any client that sends RFC 8707 `resource`.

## Known limitations

- **No `WWW-Authenticate` header on 401s.** API Gateway HTTP API's native
  JWT authorizer can't emit custom response headers. Fixing this would
  require swapping to a Lambda authorizer — a bigger, more invasive change
  to auth-critical code.
- **`resource` isn't bound to the token's `aud` claim.** Cognito access
  tokens carry no `aud` claim at all (see `docs/design-notes.md` §3.1); API
  Gateway's JWT authorizer works around this by matching `audience` against
  the token's `client_id` claim instead. A Cognito Pre Token Generation
  Lambda trigger could bind `resource` → `aud`, but this is moot while
  `resource` is simply dropped rather than honored (see above).
