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
  description = "OAuth redirect URIs allowed to complete the Cognito Hosted UI login flow. Defaults to Claude.ai's and Claude.com's MCP connector callbacks — override with -var if connecting a different AI tool with custom MCP server support."
  type        = list(string)
  default = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
  ]
}

variable "mcp_lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the MCP Lambda (Layer 2 rate limiting, design-notes.md §3.4). Defaults to -1 (AWS's sentinel for no reservation) so `terraform apply` works out of the box — most accounts start on the default 10-unit account-wide Concurrent Executions quota, which has no headroom to reserve any amount above 0. See README.md 'Lambda concurrency' for how to request a quota increase and enable a real reservation once approved."
  type        = number
  default     = -1
}
