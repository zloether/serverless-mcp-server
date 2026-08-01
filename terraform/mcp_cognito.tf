# ---------------------------------------------------------------------------
# Cognito — auth for the MCP server
# (see docs/design-notes.md §3.1 / build order step 1)
#
# Standalone on purpose: no Lambda or API Gateway wiring here yet. Verify the
# Hosted UI login manually and inspect a real access token (see the design
# notes' gotcha about the missing `aud` claim) before building anything on
# top.
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

  callback_urls = var.mcp_oauth_callback_urls

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
