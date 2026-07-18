# serverless-mcp-server

Template for a serverless remote MCP server on AWS, deployable as a Claude.ai
custom connector: API Gateway + Lambda + Cognito OAuth, with rate limiting
(API Gateway throttling, Lambda concurrency cap, DynamoDB usage counter)
active from the start. Ships with a single `hello_world` tool that proves the
whole chain — discovery → OAuth login → token → tool call → usage counter —
works end to end before you write any real tool logic.

See [`docs/design-notes.md`](docs/design-notes.md) for the architecture and
the reasoning behind each decision. See [`AGENTS.md`](AGENTS.md) for repo
conventions.

## Prerequisites

- An AWS account and credentials configured locally (`aws configure` or
  equivalent)
- Terraform >= 1.10
- Python 3.13, for running the test suite locally
- An S3 bucket for Terraform state (create one first — see below)

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
     --username you@example.com
   ```

5. **Verify Hosted UI login manually** before connecting Claude.ai: visit
   the URL from `terraform output mcp_cognito_hosted_ui_domain` +
   `/login?client_id=<app_client_id>&response_type=code&redirect_uri=https://claude.ai/api/mcp/auth_callback`,
   log in, set a password, complete MFA enrollment (TOTP or passkey).

6. **Add the custom connector in Claude.ai:** Settings → Connectors → Add
   custom connector.
   - Server URL: `terraform output -raw mcp_server_url`
   - Advanced settings → OAuth Client ID: `terraform output -raw mcp_cognito_app_client_id`
   - OAuth Client Secret: `terraform output -raw mcp_cognito_app_client_secret`

7. **Test it** — ask Claude to call the `hello_world` tool. A successful
   round trip confirms the entire chain works.

## Adding real tools

Once step 7 works, replace the `hello_world` stub — see
[`lambdas/hello-mcp/README.md`](lambdas/hello-mcp/README.md#adding-a-real-tool).

## Running tests locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check .
```
