# Serverless MCP Server — Design Notes

Why this repo is built the way it is, and what to check before you build real
tools on top of it. Decisions locked in for this template: **AWS API Gateway
+ Lambda**, **real OAuth login via Amazon Cognito Hosted UI** (not a minimal
auto-approve shim).

---

## 1. Why a remote MCP server, not code run inline in the conversation

Ruled out before settling on this design:

- Claude.ai's built-in code execution ("analysis tool") is sandboxed with no
  general internet egress — it cannot make outbound HTTP calls to an
  arbitrary host (your API Gateway URL), on web or mobile.
- Uploading a script to a conversation makes its *text* available as context
  for Claude to read — it is not executed with network access either. Same
  ceiling as pasting the code inline.
- The supported mechanism for Claude.ai to call an external, authenticated
  API live during a conversation is a **remote MCP server**, added as a
  **Custom Connector** (Settings → Connectors, Pro/Max plans and up;
  Organization settings → Connectors on Team/Enterprise). This is a
  one-time account-level connection, not something re-established per
  prompt.

---

## 2. Claude.ai custom connector requirements

- Adding a connector needs only a server URL for dev/testing (no auth) — not
  suitable for anything beyond a throwaway test, since the URL alone grants
  access.
- Under "Advanced settings" you can manually enter an **OAuth Client ID and
  Client Secret** — Dynamic Client Registration (RFC 7591) is *not* required
  if credentials are supplied this way. This is what makes Cognito viable
  without building a DCR shim (Cognito has no native DCR endpoint).
- The server must implement the **MCP Streamable HTTP transport** (JSON-RPC
  over POST, optionally streaming) — not the older HTTP+SSE transport. This
  template implements it in stateless mode: one JSON response per POST, no
  SSE, no session tracking, which maps directly onto a single Lambda
  invocation per request.
- OAuth callback URLs must allowlist both `claude.ai` and `claude.com`
  redirect endpoints on the app client / OAuth provider config.

---

## 3. Architecture

```
Claude.ai (web/mobile)
   │  OAuth login (Cognito Hosted UI) → bearer token
   │  MCP JSON-RPC over HTTPS (Streamable HTTP)
   ▼
API Gateway (HTTP API)  ── JWT authorizer (validates Cognito token)
   │  ANY /mcp → Lambda proxy integration
   │  GET /.well-known/oauth-authorization-server → static discovery JSON
   ▼
Lambda (Python) — MCP server (hand-rolled JSON-RPC: initialize / tools/list / tools/call)
   │
   ├─ DynamoDB (on-demand) — Layer 3 cumulative usage counter
   └─ Outbound calls to whatever data source a real tool needs
```

No `mcp` SDK dependency: that SDK's Streamable HTTP transport is built for a
long-lived ASGI/stdio server, not a single-shot Lambda invocation, and
there's no ready adapter between the two — hence the hand-rolled dispatcher
in `handler.py`.

### 3.1 Auth — Amazon Cognito

- One User Pool, **self-signup disabled**. Single user created via
  `AdminCreateUser` — no public registration surface.
- Hosted UI enabled on Cognito's default domain (`your-prefix.auth.<region>.
  amazoncognito.com`) — skip a custom domain/ACM cert, not worth it for a
  handful of users.
- One **confidential** App Client (has a secret), Authorization Code grant.
  Callback URLs: Claude.ai's and Claude.com's OAuth redirect URIs.
- Paste the App Client ID/secret and the Hosted UI's `/oauth2/authorize` and
  `/oauth2/token` URLs into Claude.ai's connector setup, in Advanced
  settings.
- **Known gotcha, already solved in `terraform/mcp_server.tf`:** Cognito
  access tokens carry no `aud` claim, which trips up API Gateway's built-in
  JWT authorizer (which expects to validate against an audience). Fix:
  validate against the token's `client_id` claim instead — API Gateway's JWT
  authorizer does this automatically when you set `audience` to the app
  client ID:

  ```hcl
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.mcp_server.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.mcp_server.id}"
  }
  ```

- MFA is enforced (`mfa_configuration = "ON"`): TOTP and passkeys/WebAuthn
  both available. Passkey enrollment only shows up in Cognito's newer
  Managed Login UI (`managed_login_version = 2` on the domain, plus a
  `aws_cognito_managed_login_branding` resource for the app client — without
  one, the client has no branding at all and passkey enrollment silently
  doesn't appear). Both are already wired up in `mcp_cognito.tf`.

### 3.2 Compute/routing

- **API Gateway HTTP API** (not REST API) — cheaper: $1.00/million requests
  vs $3.50 for REST API.
- `ANY /mcp` → Lambda proxy integration. One Lambda implements the MCP
  protocol surface (`initialize`, `tools/list`, `tools/call`).
- A route for `GET /.well-known/oauth-authorization-server`, pointing at the
  Cognito Hosted UI's authorize/token endpoints, so Claude.ai's OAuth
  discovery step succeeds. No auth in front of it — it's public metadata.

### 3.3 Data layer

Add whatever a real tool needs (SSM Parameter Store for API keys, DynamoDB
for a response cache, etc.) — this template ships only the Layer 3 usage
counter table, since the `hello_world` tool has no upstream dependency.

- **SSM Parameter Store** (SecureString) is the cheap default for
  third-party API keys — avoids Secrets Manager's $0.40/secret/month;
  Parameter Store standard tier is free.
- If a real tool benefits from caching (e.g. the same input queried
  repeatedly in a session), add a DynamoDB table with a TTL attribute rather
  than re-fetching every call.
- Scope the Lambda execution role to exactly the resources a real tool
  needs — nothing broader.

### 3.4 Rate limiting & hard usage caps

API Gateway + Lambda + Cognito is built to scale to millions of requests;
a personal or low-traffic MCP server needs the opposite — every layer below
is sized to sit comfortably above realistic use and comfortably below
anything that could run up meaningful cost. The goal: invisible under normal
use, impossible to miss if something's wrong, self-limiting without
requiring you to notice and react.

Rate limiting (requests/second) and cumulative caps (requests/day) are
different problems — a slow, steady leak clears a rate limit but still adds
up over a month. This template covers both, plus a note on the unknown.

**Layer 1 — API Gateway throttling (per-second rate)**
Route-level throttle on `ANY /mcp`: steady-state **2 req/s, burst 5**
(`terraform/mcp_server.tf`). Cheap, config-only, independent of anything the
Lambda code does. Bounds worst-case request rate regardless of caller
behavior. The same throttle is applied to the unauthenticated discovery
route, since it has no auth in front of it.

**Layer 2 — Lambda reserved concurrency (parallelism cap)**
`var.mcp_lambda_reserved_concurrency` caps concurrent executions on the MCP
Lambda — requests beyond that are throttled (429) instead of executing, a
second, independent backstop if the API Gateway throttle is ever
misconfigured or bypassed, and a hard ceiling on cost-per-unit-time
regardless of request rate. Defaults to `-1` (no reservation) because most
AWS accounts start on a 10-unit account-wide concurrency floor with no
headroom to reserve any amount above 0 — see README.md "Lambda concurrency"
for requesting a quota increase and setting a real reservation (e.g. `2`)
once it's approved.

**Layer 3 — Application-level cumulative cap (the actual kill switch)**
A DynamoDB counter table (`usage-counters`, PK `date#YYYY-MM-DD` and
`month#YYYY-MM`), atomically incremented (`UpdateItem` + `ADD`) on every
tool invocation (`usage_cap.py`). Before doing any real work, the Lambda
checks the counter; if the threshold is exceeded it short-circuits and
returns an MCP error ("usage cap reached") instead of proceeding.

- Defaults in this template: **200/day, 2,000/month** — adjust
  `DAILY_LIMIT`/`MONTHLY_LIMIT` in `terraform/mcp_server.tf` to your
  expected usage.
- Enforced in code, so it doesn't depend on AWS billing data (which can lag
  hours) — it's the fastest-acting layer.
- Breaches fail silently otherwise (caller just gets an MCP error) — wire a
  CloudWatch alarm off the counter (or a metric filter on the "usage cap
  reached" log line) if you want to be notified the moment a cap is hit,
  rather than only via a billing alarm hours later.

**Layer 4 — Per-provider caps (protects paid data sources specifically)**
If a real tool calls a metered third-party API, track a separate counter per
provider (same DynamoDB table, different PK prefix) and cap it
independently — e.g. 50 calls/day to any paid provider. Not needed for the
`hello_world` stub; add it when you add a tool that costs money per call.

**Layer 5 — Token/session hygiene (limits blast radius of a leaked credential)**
The Cognito app client's token TTLs are already shortened in
`mcp_cognito.tf`: access token 1 hour, refresh token **7 days** (down from
the 30-day default). If you suspect a leak, revoke immediately with
`AdminUserGlobalSignOut` or `RevokeToken` rather than waiting on any cap
above to catch it.

**Layer 6 — Cost-anomaly kill switch (defense against the unknown)**
Not included in this template — add an AWS Budget at a low threshold (e.g.
$5/month) with a **Budget Action** attached (not just an email alert), so a
cost spike from something the request-based counters didn't anticipate
(e.g. CloudWatch Logs ingestion) auto-denies the Lambda role or disables
the API Gateway stage.

**Layer 7 — WAF rate-based rule (optional, real cost tradeoff)**
An unauthenticated request still costs a fraction of a cent at API Gateway
before the Cognito authorizer rejects it. AWS WAF in front of the HTTP API,
one rate-based rule blocking any single IP over ~100 requests/5 min, closes
that gap — but adds a flat ~$5–8/month base cost, large relative to the
rest of this stack. Skip initially; revisit only if the discovery
endpoint's logs show unexpected unauthenticated traffic.

**Summary**

| Layer | Mechanism | Default limit | Response to breach | Included here? |
|---|---|---|---|---|
| Rate | API Gateway route throttle | 2 req/s, burst 5 | 429, request dropped | Yes |
| Concurrency | Lambda reserved concurrency | None by default (`-1`); set after quota increase | 429, request throttled | Yes |
| Cumulative (app-wide) | DynamoDB counter, checked pre-execution | 200/day, 2,000/month | MCP error, no downstream calls made | Yes |
| Cumulative (per-provider) | DynamoDB counter, checked pre-provider-call | 50/day per paid provider | MCP error for that tool only | No — add when you add a paid tool |
| Credential blast radius | Cognito token TTL | 1h access / 7d refresh | Leaked token expires fast; revocable on demand | Yes |
| Cost anomaly | AWS Budget + Budget Action | $5/month | Auto-deny Lambda role or disable API stage | No — add before real production use |
| Edge flood (optional) | AWS WAF rate-based rule | ~100 req/5min per IP | Blocked at the edge, pre-billing | No |

CloudWatch Logs retention is capped at 14 days in this template (`retention_in_days = 14` on both log groups) — the same "bounded by default" principle applied to log storage cost.

### 3.5 Cost estimate at low-traffic personal scale

| Service | Expected cost |
|---|---|
| Lambda | $0 — well inside the always-free 1M requests + 400K GB-s/month |
| API Gateway (HTTP API) | $0–1/month |
| Cognito | $0 — free under 10K MAUs |
| DynamoDB (on-demand) | $0 — inside the always-free 25GB / 25 WCU-RCU tier |
| CloudWatch | Low cents |
| **Total** | **~$0–2/month** |

---

## 4. Build order (recommended for anyone forking this template)

1. **Cognito first, standalone.** `terraform apply` just `mcp_cognito.tf`'s
   resources (comment out or remove `mcp_server.tf` temporarily if you want
   to isolate it). Verify login manually in a browser (hit the Hosted UI
   URL, log in, confirm a token comes back) before touching the Lambda.
2. **Full apply with the `hello_world` stub**, which is what this template
   ships with Layers 1–3 already active. Connect as a real Claude.ai custom
   connector and confirm the *entire chain* — discovery → OAuth login →
   token → `hello_world` tool call → usage counter increments — works end
   to end. This is the highest-risk unknown; get it working before writing
   any real tool logic.
3. **Replace `hello_world` with your first real tool.** See
   `lambdas/hello-mcp/README.md`'s "Adding a real tool" section.
4. **Repeat for additional tools**, each behind its own cache/cap as needed
   (§3.3–3.4).
