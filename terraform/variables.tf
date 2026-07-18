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
