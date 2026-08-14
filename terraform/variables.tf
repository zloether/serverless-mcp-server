variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name applied as a tag to all resources"
  type        = string
  default     = "serverless-mcp-server"
}

variable "mcp_cognito_domain_prefix" {
  description = "Cognito Hosted UI domain prefix for the MCP server user pool — must be globally unique across all AWS accounts in the region (full domain: <prefix>.auth.<region>.amazoncognito.com). Override with -var if the default is taken."
  type        = string
  default     = "serverless-mcp"
}

variable "mcp_oauth_callback_urls" {
  description = "Additional OAuth redirect URIs to allow alongside Claude.ai's and Claude.com's default MCP connector callbacks (see locals.mcp_default_oauth_callback_urls in mcp_cognito.tf) — set via dev.auto.tfvars to connect another AI tool (e.g. ChatGPT, Postman) without losing Claude's callbacks."
  type        = list(string)
  default     = []
}

variable "mcp_lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the MCP Lambda (Layer 2 rate limiting, design-notes.md §3.4). Defaults to -1 (AWS's sentinel for no reservation) so `terraform apply` works out of the box — most accounts start on the default 10-unit account-wide Concurrent Executions quota, which has no headroom to reserve any amount above 0. See README.md 'Lambda concurrency' for how to request a quota increase and enable a real reservation once approved."
  type        = number
  default     = -1
}

variable "mcp_verbose_oauth_logging" {
  description = "When true, the OAuth proxy routes (/oauth2/authorize, /oauth2/token) log full request/response headers to CloudWatch in addition to the redacted bodies they always log. Off by default — headers are noisy and rarely needed once a flow is known-working; turn on temporarily while debugging a connector issue. See docs/DEBUGGING.md."
  type        = bool
  default     = false
}

variable "mcp_strip_oauth_params" {
  description = "OAuth query/body param names for the /oauth2/authorize and /oauth2/token proxy routes to drop before forwarding to Cognito. Empty by default — Cognito has a resource server registered (mcp_cognito.tf) and accepts standard params, including RFC 8707's `resource`, natively via resource binding, so nothing needs stripping out of the box. Set to e.g. [\"resource\"] to drop specific params again if a differently-configured pool rejects something a client sends. See docs/chatgpt-oauth-notes.md."
  type        = list(string)
  default     = []
}
