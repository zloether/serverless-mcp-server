# ---------------------------------------------------------------------------
# Cognito — auth for the MCP server
# (see docs/design-notes.md §3.1 / build order step 1)
#
# Not standalone: the resource server below depends on the API Gateway
# declared in mcp_server.tf (its `identifier` references
# aws_apigatewayv2_api.mcp_server.api_endpoint), so this file can't be
# applied in isolation. Verify the Hosted UI login manually and inspect a
# real access token (see the design notes' gotcha about the missing `aud`
# claim) after a full apply, before connecting a real MCP client.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "mcp_server" {
  name = "serverless-mcp-users"

  # Sign in with email instead of an arbitrary username — simpler for the
  # single admin-created user. Immutable after creation.
  username_attributes = ["email"]

  # No public registration surface — the only user is created out-of-band via
  # `aws cognito-idp admin-create-user`.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # MFA required — TOTP and passkeys (WebAuthn) both available. Hosted UI
  # prompts enrollment (QR-code TOTP or a platform/security-key passkey)
  # the first time this user logs in after it's enforced.
  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  # relying_party_id is left unset on purpose: Cognito only accepts it once
  # it matches a domain actually associated with the pool, but
  # aws_cognito_user_pool_domain necessarily depends on this pool's ID
  # (setting it explicitly here would create a dependency cycle). Omitting
  # it makes Cognito default to the pool's prefix domain automatically.
  web_authn_configuration {
    user_verification = "required"
  }
}

resource "aws_cognito_user_pool_domain" "mcp_server" {
  domain       = var.mcp_cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.mcp_server.id

  # Passkey (WebAuthn) enrollment only exists in Cognito's newer Managed
  # Login UI, not the classic Hosted UI (version 1) — TOTP enrollment works
  # on both, which is why only TOTP was reachable before this.
  managed_login_version = 2
}

# Claude.ai/Claude.com are always allowed, independent of
# var.mcp_oauth_callback_urls — kept as a local rather than the variable's
# default so a tfvars override (e.g. dev.auto.tfvars adding ChatGPT's or
# Postman's callback) extends this set instead of replacing it, letting the
# same app client serve multiple AI tools at once.
locals {
  mcp_default_oauth_callback_urls = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
  ]
  mcp_oauth_callback_urls = distinct(concat(local.mcp_default_oauth_callback_urls, var.mcp_oauth_callback_urls))
}

resource "aws_cognito_user_pool_client" "mcp_server" {
  name         = "serverless-mcp-client"
  user_pool_id = aws_cognito_user_pool.mcp_server.id

  # Confidential client (has a secret) — Client ID/Secret get pasted into
  # Claude.ai's connector "Advanced settings", so Dynamic Client Registration
  # isn't needed.
  generate_secret = true

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.mcp_oauth_callback_urls

  # Only the authorization-code flow (above) and refresh-token exchange are
  # used. Without this, Cognito's default auth flows also allow direct SRP
  # auth against this client — still MFA-gated (pool-level MFA is required
  # below), but a second credential-exchange path this client doesn't need.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  prevent_user_existence_errors = "ENABLED"

  # Layer 5 (design-notes.md §3.4) — shrink refresh token lifetime from
  # Cognito's 30-day default to limit the blast radius of a leaked credential.
  access_token_validity  = 1
  refresh_token_validity = 7
  token_validity_units {
    access_token  = "hours"
    refresh_token = "days"
  }
}

# Resource server registered purely to enable Cognito's resource binding
# (RFC 8707 `resource` param support, Managed Login only — see
# docs/chatgpt-oauth-notes.md). No custom scopes needed: registering the
# identifier is what makes Cognito accept `resource=<identifier>` at
# /oauth2/authorize instead of rejecting the token exchange with
# invalid_grant, and it makes Cognito set the access token's `aud` claim to
# this value. Identifier must exactly match the `resource` value clients
# send (ChatGPT's connector sends `${api_endpoint}/mcp`, i.e. MCP_SERVER_URL).
# The API Gateway JWT authorizer's `audience` list (mcp_server.tf) includes
# this identifier alongside the app client ID, since it's what `aud` becomes
# for any client that sends `resource` — forwarded by default now that this
# resource server exists (var.mcp_strip_oauth_params).
resource "aws_cognito_resource_server" "mcp_server" {
  identifier   = "${aws_apigatewayv2_api.mcp_server.api_endpoint}/mcp"
  name         = "MCP server"
  user_pool_id = aws_cognito_user_pool.mcp_server.id
}

# Managed Login (managed_login_version = 2 above) renders pages per app
# client from a branding record — without one, the client has no branding
# at all (confirmed via `aws cognito-idp describe-managed-login-branding-by-client`
# returning ResourceNotFoundException), which is why passkey enrollment
# still didn't show up after switching the domain's UI version alone.
resource "aws_cognito_managed_login_branding" "mcp_server" {
  user_pool_id                = aws_cognito_user_pool.mcp_server.id
  client_id                   = aws_cognito_user_pool_client.mcp_server.id
  use_cognito_provided_values = true
}
