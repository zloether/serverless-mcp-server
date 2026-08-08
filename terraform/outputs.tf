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

output "mcp_cognito_login_url" {
  value       = "https://${aws_cognito_user_pool_domain.mcp_server.domain}.auth.${var.aws_region}.amazoncognito.com/login?client_id=${aws_cognito_user_pool_client.mcp_server.id}&response_type=code&redirect_uri=${var.mcp_oauth_callback_urls[0]}"
  description = "Open in a browser to manually verify Hosted UI login before connecting Claude.ai (or another MCP-capable AI tool, if var.mcp_oauth_callback_urls was overridden)"
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

output "mcp_discovery_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}.well-known/oauth-authorization-server"
  description = "GET in Postman — unauthenticated OAuth authorization server metadata"
}

output "mcp_protected_resource_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}.well-known/oauth-protected-resource"
  description = "GET in Postman — unauthenticated protected resource metadata (RFC 9728)"
}

output "mcp_jwks_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}.well-known/jwks.json"
  description = "GET in Postman — unauthenticated JWKS used to verify access tokens"
}

output "mcp_oauth_authorize_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}oauth2/authorize"
  description = "GET in Postman (or a browser) — authorize proxy in front of Cognito Hosted UI login"
}

output "mcp_oauth_token_url" {
  value       = "${aws_apigatewayv2_stage.mcp_server_default.invoke_url}oauth2/token"
  description = "POST in Postman to exchange an authorization code (or refresh token) for an access token"
}
