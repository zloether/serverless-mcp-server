# Repository Overview

This repo is a template for a serverless remote MCP server on AWS: API Gateway + Lambda + Cognito OAuth, with rate-limiting baked in from the start. It proves the full connector chain (discovery → OAuth login → token → tool call → usage counter) with a trivial `hello_world` tool — swap in real tools once you've confirmed the chain works for your own AWS account and Cognito domain.

See `docs/design-notes.md` for the architecture and the reasoning behind each decision (why a remote MCP server, why Cognito, why the specific rate-limiting layers).

## Directory Structure

```
├── terraform/              # Root Terraform module — all infrastructure goes here
│   ├── backend.tf          # S3 remote state backend config
│   ├── versions.tf         # Terraform version + provider requirements
│   ├── providers.tf        # AWS provider configuration
│   ├── variables.tf        # Input variables
│   ├── main.tf             # Shared/ungrouped resource definitions (intentionally sparse)
│   ├── mcp_cognito.tf      # Cognito user pool, Hosted UI, app client (OAuth for the MCP connector)
│   ├── mcp_server.tf       # MCP server: API Gateway, Lambda, rate-limit Layers 1-3, usage-counter table
│   └── outputs.tf          # Outputs — Cognito Hosted UI URL, client ID/secret, MCP server URL, plus Postman/debugging URLs (discovery, protected-resource, authorize, token, JWKS)
├── lambdas/
│   └── hello-mcp/          # The MCP server Lambda
│       ├── src/
│       │   ├── handler.py      # JSON-RPC dispatcher: initialize / tools/list / tools/call
│       │   ├── usage_cap.py    # Layer 3 DynamoDB cumulative usage counter
│       │   └── tools/          # One module per MCP tool — only hello_world.py here
│       └── README.md
├── modules/                # Reusable Terraform modules (empty placeholder)
├── tests/                  # pytest suite for the Lambda handler
├── scripts/build_lambda.sh # Packaging script for Lambdas with a requirements.txt (none needed here yet)
├── AGENTS.md               # This file
├── CLAUDE.md, GEMINI.md,   # AI-tool instruction files — all point to this one.
│   .windsurfrules,         # Keep them in sync if this file is renamed/moved.
│   .cursor/rules/agents.mdc,
│   .github/copilot-instructions.md
├── .github/                # CI workflow (ruff + pytest) and Dependabot config
├── docs/
│   ├── design-notes.md     # Why this architecture, Claude.ai connector requirements, rate-limiting design
│   ├── DEBUGGING.md        # Testing the OAuth + MCP chain directly with Postman/curl
│   └── chatgpt-oauth-notes.md  # How ChatGPT's connector does OAuth and what this template does to support it
├── LICENSE                 # Apache 2.0 + Commons Clause
├── pyproject.toml          # ruff config
├── requirements-dev.txt    # boto3, pytest, ruff
└── README.md               # Human-facing setup/deploy guide
```

## Terraform State

- **Backend:** S3 with native state locking (`use_lockfile = true`)
- **Region:** `us-east-1` (change `var.aws_region` if you want a different one)
- **State key:** `tf/terraform.tfstate`
- The bucket name in `terraform/backend.tf` is a placeholder (`CHANGE-ME-...`) — create an S3 bucket and fill in the real name before running `terraform init`. Backend config does not support variables; the bucket name, key, and region must be literals.

## Versions & Providers

- Terraform >= 1.10 required (for S3 native state locking)
- AWS provider `~> 6.50`

## Workflow

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

After `apply`, see the README's setup steps for creating the Cognito user and connecting Claude.ai as a custom connector.

## Lambdas

- Lambda source code lives under `lambdas/<name>/src/`.
- Each Lambda directory has a `README.md` at `lambdas/<name>/README.md` covering: one-paragraph description, **Environment Variables** table, **Dependencies**, **Building**, **Payload Format**.
- Use `logger = logging.getLogger()` (not `log`) as the module-level logger, with `logger.setLevel(logging.INFO)` on the next line.
- Prefer `arm64` architecture (`architectures = ["arm64"]`); use `manylinux2014_aarch64` when building native dependencies.
- Runtime is `python3.13`. Handler entrypoint is always `handler.handler` (file `handler.py`, function `def handler(event, context):`).
- Use `%s` placeholders in log calls, not f-strings: `logger.info("msg: %s", value)`.
- Order imports: stdlib (alphabetical) → blank line → third-party.

### Lambda packaging

- If a Lambda has no `requirements.txt`, use `archive_file` directly from `src/` (this is what `hello-mcp` does).
- If a Lambda has a `requirements.txt`, use a `null_resource` to run `build_lambda.sh`, then point `archive_file` at `build/<name>/`. Rebuild triggers are the sha256 of all `.py` files in `src/` plus the sha256 of `requirements.txt`.

## Tagging

All taggable AWS resources receive two tags automatically via `default_tags` in `providers.tf`:

| Tag | Value |
|-----|-------|
| `repo` | `serverless-mcp-server` |
| `project` | `var.project` (default `"serverless-mcp-server"`) |

Do not add `repo` or `project` to individual resource `tags` blocks unless overriding `project` for a specific resource. Do not remove `default_tags` from the provider.

## IAM

- Use inline `aws_iam_role_policy` with policy JSON built from a `data "aws_iam_policy_document"` — not a separate `aws_iam_policy` resource.
- Scope permissions to specific resource ARNs. Use `"*"` only where AWS does not support resource-level conditions.
- Every Lambda role includes a CloudWatch Logs statement: actions `["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]`, resource scoped to that function's own log group ARN plus a trailing `:*` for the log-stream level (e.g. `"${aws_cloudwatch_log_group.mcp_server_lambda.arn}:*"`), not a wildcard.

## Conventions

- Group related resources into a dedicated `.tf` file rather than putting everything in `main.tf`.
- Reusable modules live under `modules/<name>/` and are called from `terraform/main.tf`.
- No `.tfvars` files are committed (excluded by `.gitignore`). Pass variable overrides via `-var` flags or environment variables (`TF_VAR_*`).
- AWS resource names (Lambda functions, IAM roles/policies, DynamoDB tables) use kebab-case.

## What Not To Do

- Do not add a DynamoDB table for state locking — S3 native locking is in use.
- Do not commit `.tfstate` files, `.tfvars` files, or `.terraform/` directories — all are in `.gitignore`.
- Do not run `terraform apply` without first reviewing `terraform plan` output.
- Do not remove the rate-limiting layers (API Gateway throttle, Lambda reserved concurrency, DynamoDB usage cap) when adding real tools — they're cheap and are the whole point of building them in before any real logic exists. See `docs/design-notes.md` §3.4.
