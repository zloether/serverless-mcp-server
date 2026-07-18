output "mcp_cognito_hosted_ui_domain" {
  value       = "https://${aws_cognito_user_pool_domain.mcp_server.domain}.auth.${var.aws_region}.amazoncognito.com"
  description = "Cognito Hosted UI base URL — append /oauth2/authorize and /oauth2/token for Claude.ai's connector setup and /login for manual browser verification"
}

output "mcp_cognito_user_pool_id" {
  value       = aws_cognito_user_pool.mcp_server.id
  description = "User pool ID — needed for the AdminCreateUser CLI call"
}

output "mcp_cognito_app_client_id" {
  value       = aws_cognito_user_pool_client.mcp_server.id
  description = "Paste into Claude.ai's custom connector Advanced settings as the OAuth Client ID"
}

output "mcp_cognito_app_client_secret" {
  value       = aws_cognito_user_pool_client.mcp_server.client_secret
  description = "Paste into Claude.ai's custom connector Advanced settings as the OAuth Client Secret"
  sensitive   = true
}

output "mcp_server_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}mcp"
  description = "Paste into Claude.ai's custom connector setup as the MCP server URL"
}
