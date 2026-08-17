# serverless-mcp-server

[MCP (Model Context Protocol)](https://modelcontextprotocol.io/) is the open
standard AI tools like Claude use to call external tools over a standard
API — a "remote MCP server" is one you host yourself and connect to Claude.ai
(or another MCP-capable tool) as a custom connector, instead of running
locally on your machine.

Template for a serverless remote MCP server on AWS, deployable as a Claude.ai
custom connector: API Gateway + Lambda + Cognito OAuth, with rate limiting
(API Gateway throttling, Lambda concurrency cap, DynamoDB usage counter)
active from the start. Ships with a single `hello_world` tool that proves the
whole chain — discovery → OAuth login → token → tool call → usage counter —
works end to end before you write any real tool logic.

See [`docs/design-notes.md`](docs/design-notes.md) for the architecture and
the reasoning behind each decision. See [`AGENTS.md`](AGENTS.md) for repo
conventions.

Tested end to end with both Claude.ai's and ChatGPT's custom connector
support (see `docs/chatgpt-oauth-notes.md` for ChatGPT-specific details). It
implements the standard MCP Streamable HTTP transport with OAuth, so it
should work with any other AI tool that supports custom MCP servers too —
just untested beyond these two. Claude.ai's and Claude.com's OAuth redirect
URIs are always allowed (`locals.mcp_default_oauth_callback_urls` in
`mcp_cognito.tf`); set `var.mcp_oauth_callback_urls` via `-var` (or a
`.tfvars` file) to add another tool's redirect URI alongside them, not
instead of them — the same app client can serve multiple AI tools at once.

**A note on the OAuth discovery `issuer`:** this server's
`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource` documents deliberately advertise
this API's own base URL as the `issuer`, not Cognito's real issuer — this is
intentional, not an oversight, so that spec-compliant clients keep resolving
OAuth endpoints through this server's proxy routes (for CloudWatch
visibility) instead of jumping straight to Cognito. The tradeoff: Cognito's
issued tokens still carry Cognito's real `iss` claim, so a client that
cross-checks a token's `iss` against the discovery document would see a
mismatch. See `docs/chatgpt-oauth-notes.md` for the full reasoning.

For example, to connect **ChatGPT**: enable Developer mode first (Settings →
Apps & Connectors → Advanced settings) — it's off by default and required to
add a custom MCP connector. ChatGPT shows a callback URL when you start
adding the connector, unique per connector, shaped like
`https://chatgpt.com/connector/oauth/<connector-id>`; set that as
`mcp_oauth_callback_urls` before finishing the connection. When prompted for
the OAuth client type, use "User-Defined OAuth Client" with token endpoint
auth method `client_secret_post` (both ChatGPT's defaults) — matches this
template's confidential Cognito app client.

ChatGPT's connector sends an OAuth `resource` parameter (RFC 8707) alongside
PKCE, which Cognito rejects with `invalid_grant` unless a matching resource
server is registered for it. `terraform/mcp_cognito.tf` already registers
one, so this works out of the box, standards-compliant — nothing is stripped
from the request by default. If a client sends some other param a
differently-configured Cognito pool rejects, set
`var.mcp_strip_oauth_params` (e.g. `["resource"]`) to drop it before
forwarding. See [`docs/chatgpt-oauth-notes.md`](docs/chatgpt-oauth-notes.md)
for the full explanation of how ChatGPT's connector does OAuth and why.

## Prerequisites

- An AWS account and credentials configured locally (`aws configure` or
  equivalent)
- Terraform >= 1.10
- Python 3.13, for running the test suite locally
- An S3 bucket for Terraform state (create one first — see below)

## Running tests locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check .
```

## Setup

1. **Create the state bucket** and fill in its name in `terraform/backend.tf`
   (replace the `CHANGE-ME-...` placeholder):

   ```bash
   aws s3api create-bucket --bucket <your-bucket-name> --region us-east-1
   aws s3api put-bucket-versioning --bucket <your-bucket-name> \
     --versioning-configuration Status=Enabled
   ```

2. **Pick a Cognito Hosted UI domain prefix** — must be globally unique
   across all AWS accounts in the region. Override the default via
   `-var mcp_cognito_domain_prefix=<your-prefix>` if `serverless-mcp` is
   taken.

3. **Deploy:**

   ```bash
   cd terraform
   terraform init
   terraform plan
   terraform apply
   ```

4. **Create a Cognito user** (no public sign-up — this pool only allows
   admin-created users):

   ```bash
   aws cognito-idp admin-create-user \
     --user-pool-id "$(terraform output -raw mcp_cognito_user_pool_id)" \
     --username you@example.com \
     --desired-delivery-mediums EMAIL
   ```

   Cognito emails a temporary password to that address (via its built-in
   email service — no SES setup needed, capped at ~50 emails/day). You'll
   need it for step 5.

5. **Verify Hosted UI login manually** before connecting Claude.ai:

   ```bash
   terraform output -raw mcp_cognito_login_url
   ```

   Open the printed URL, log in with the temporary password from step 4,
   set a permanent password when prompted, then complete MFA enrollment
   (TOTP or passkey).

6. **Add the custom connector in Claude.ai:** Settings → Connectors → Add
   custom connector. **Must be done from a web browser** (claude.ai) — the
   mobile app doesn't support adding custom connectors.
   - Server URL: `terraform output -raw mcp_server_url`
   - Advanced settings → OAuth Client ID: `terraform output -raw mcp_cognito_app_client_id`
   - OAuth Client Secret: `terraform output -raw mcp_cognito_app_client_secret`

7. **Test it** — ask Claude to call the `hello_world` tool. A successful
   round trip confirms the entire chain works.

## Adding real tools

Once step 7 works, replace the `hello_world` stub — see
[`lambdas/hello-mcp/README.md`](lambdas/hello-mcp/README.md#adding-a-real-tool).

## Cost considerations

At low-traffic personal scale (a handful of users, well under the rate
limits in [`docs/design-notes.md`](docs/design-notes.md) §3.4), everything
here should stay inside AWS's always-free tier:

| Service | Expected cost |
|---|---|
| Lambda | $0 — well inside the always-free 1M requests + 400K GB-s/month |
| API Gateway (HTTP API) | $0–1/month |
| Cognito | $0 — free under 10K MAUs |
| DynamoDB (on-demand) | $0 — inside the always-free 25GB / 25 WCU-RCU tier |
| CloudWatch | Low cents |
| **Total** | **~$0–2/month** |

The $0–2/month figure assumes normal use. The five unauthenticated OAuth
routes (discovery, protected-resource metadata, authorize, token, JWKS) have
no authorizer in front of them, so they're bounded only by the API Gateway
per-route throttle, not by actual traffic — this is exactly why the AWS
Budget below matters.

This template's rate-limiting layers (API Gateway throttle, Lambda
concurrency cap, DynamoDB usage counter) exist specifically to keep it there
— see §3.4 for the full breakdown, including what's *not* included by
default (a cost-anomaly AWS Budget with a Budget Action, and an optional WAF
rate-based rule).

- **Set a low-threshold AWS Budget** (e.g. $5/month) as a backstop against
  the unknown — request-based counters can't catch cost from something they
  didn't anticipate (e.g. a CloudWatch Logs ingestion spike).
- **`terraform destroy`** tears everything down if you're done testing —
  nothing here has a minimum commitment or termination fee.

## Token lifetime

Cognito issues a short-lived access token plus a longer-lived refresh
token (Layer 5, [`docs/design-notes.md`](docs/design-notes.md) §3.4):

- **Access token: 1 hour** — used per-request; Claude.ai refreshes it
  silently, no reauthorization needed.
- **Refresh token: 7 days** — once this expires, Claude.ai can no longer
  silently refresh and you'll need to log back in through the Hosted UI
  (step 5) to reauthorize the connector.

7 days is a reasonable default for a personal/demo deployment — it limits
how long a leaked credential stays useful. To extend it (e.g. so you don't
have to reauthorize as often), edit the `aws_cognito_user_pool_client` block
in `mcp_cognito.tf`:

```hcl
access_token_validity  = 1
refresh_token_validity = 30  # was 7
token_validity_units {
  access_token  = "hours"
  refresh_token = "days"
}
```

Cognito allows `refresh_token_validity` up to `3650` days (10 years). Longer
refresh tokens mean less friction but a bigger blast radius if one leaks —
weigh that tradeoff for your own deployment.

## Lambda concurrency

`var.mcp_lambda_reserved_concurrency` (Layer 2 rate limiting, see
[`docs/design-notes.md`](docs/design-notes.md) §3.4) defaults to `-1` — AWS's
sentinel for "no reservation" — so `terraform apply` works out of the box.
AWS always keeps at least **10 units unreserved** account-wide, no matter
how high your account's total "Concurrent executions" quota is (1,000 by
default, but new/low-usage accounts are sometimes provisioned well below
that). If your account's total quota is at or near that 10-unit floor,
reserving any amount above `0` for this Lambda fails with:

```
InvalidParameterValueException: Specified ReservedConcurrentExecutions for
function decreases account's UnreservedConcurrentExecution below its
minimum value of [10].
```

To enable a real per-function cap:

1. **Request a quota increase** for Lambda "Concurrent executions" (1,000
   gives plenty of headroom):

   ```bash
   aws service-quotas request-service-quota-increase \
     --service-code lambda \
     --quota-code L-B99A9384 \
     --desired-value 1000
   ```

   Or via the console: Service Quotas → AWS services → Lambda → Concurrent
   executions → Request increase. This can take anywhere from a few minutes
   to a day or two to be approved.

2. **Once approved**, set a real reservation (`2` is what this template was
   designed around) and re-apply:

   ```bash
   cd terraform
   terraform apply -var mcp_lambda_reserved_concurrency=2
   ```

   Or add `mcp_lambda_reserved_concurrency = 2` to a `.tfvars` file so you
   don't have to pass `-var` on every apply.

## Debugging the OAuth connection

If a connector fails to authenticate, see
[`docs/DEBUGGING.md`](docs/DEBUGGING.md) for testing the OAuth + MCP chain
directly with Postman — useful for isolating whether a failure is this
server or the client's OAuth implementation.

## License

Apache 2.0 with the [Commons Clause](https://commonsclause.com/) (see
[`LICENSE`](LICENSE)). Free to use, modify, and deploy — including for
commercial purposes — but you can't sell the software itself or offer it
(or a service substantially derived from it) to third parties for a fee,
e.g. hosting this as a paid product.
