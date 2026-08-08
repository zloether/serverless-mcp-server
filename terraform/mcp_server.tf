# ---------------------------------------------------------------------------
# MCP server — Lambda + API Gateway, with rate-limiting Layers 1-3 active
# from the start (docs/design-notes.md §3.2-3.4 / §4 build order step 2).
# The Lambda here only exposes a single `hello_world` tool — swap in real
# tools under lambdas/hello-mcp/src/tools/ once the connector chain
# (discovery -> OAuth login -> token -> tool call -> usage counter) is
# confirmed working end to end.
# ---------------------------------------------------------------------------

# Layer 3 — cumulative usage cap counter
resource "aws_dynamodb_table" "mcp_usage_counters" {
  name         = "usage-counters"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "counter_id"

  attribute {
    name = "counter_id"
    type = "S"
  }
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "mcp_server_lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp_server_lambda" {
  name               = "serverless-mcp-server-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.mcp_server_lambda_assume.json
}

data "aws_iam_policy_document" "mcp_server_permissions" {
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.mcp_usage_counters.arn]
  }
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }
}

resource "aws_iam_role_policy" "mcp_server_lambda" {
  name   = "serverless-mcp-server-lambda-policy"
  role   = aws_iam_role.mcp_server_lambda.id
  policy = data.aws_iam_policy_document.mcp_server_permissions.json
}

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------
data "archive_file" "mcp_server_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/hello-mcp/src"
  output_path = "${path.module}/../build/hello-mcp.zip"
  excludes    = ["__pycache__"]
}

resource "aws_lambda_function" "mcp_server" {
  function_name    = "serverless-mcp-server"
  filename         = data.archive_file.mcp_server_lambda.output_path
  source_code_hash = data.archive_file.mcp_server_lambda.output_base64sha256
  architectures    = ["arm64"]
  handler          = "handler.handler"
  runtime          = "python3.13"
  role             = aws_iam_role.mcp_server_lambda.arn
  # Kept below API Gateway's 30s integration timeout (the hard ceiling for
  # HTTP APIs, not configurable higher) so a slow request gets a clean
  # Lambda timeout instead of racing a gateway 504. Raise this if a real
  # tool does slower upstream work than the hello_world stub.
  timeout     = 10
  memory_size = 128

  # Layer 2 (design-notes.md §3.4) — cap parallelism. Defaults to no
  # reservation (see var.mcp_lambda_reserved_concurrency) until you request
  # a concurrency quota increase — see README.md "Lambda concurrency".
  reserved_concurrent_executions = var.mcp_lambda_reserved_concurrency

  environment {
    variables = {
      USAGE_TABLE_NAME      = aws_dynamodb_table.mcp_usage_counters.name
      DAILY_LIMIT           = "200"
      MONTHLY_LIMIT         = "2000"
      HOSTED_UI_DOMAIN      = "https://${aws_cognito_user_pool_domain.mcp_server.domain}.auth.${var.aws_region}.amazoncognito.com"
      COGNITO_ISSUER        = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.mcp_server.id}"
      MCP_SERVER_URL        = "${aws_apigatewayv2_api.mcp_server.api_endpoint}/mcp"
      API_BASE_URL          = aws_apigatewayv2_api.mcp_server.api_endpoint
      VERBOSE_OAUTH_LOGGING = tostring(var.mcp_verbose_oauth_logging)
    }
  }
}

resource "aws_cloudwatch_log_group" "mcp_server_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.mcp_server.function_name}"
  retention_in_days = 14
}

# ---------------------------------------------------------------------------
# API Gateway (HTTP API)
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "mcp_server" {
  name          = "serverless-mcp"
  protocol_type = "HTTP"
}

# Cognito access tokens carry no `aud` claim — API Gateway's JWT authorizer
# falls back to matching `audience` against the token's `client_id` claim
# when `aud` is absent, which is exactly what's configured here. Confirmed
# working via manual Hosted UI login (see docs/design-notes.md).
resource "aws_apigatewayv2_authorizer" "mcp_server" {
  api_id           = aws_apigatewayv2_api.mcp_server.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "mcp-cognito-jwt"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.mcp_server.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.mcp_server.id}"
  }
}

resource "aws_apigatewayv2_integration" "mcp_server" {
  api_id                 = aws_apigatewayv2_api.mcp_server.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.mcp_server.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "mcp" {
  api_id             = aws_apigatewayv2_api.mcp_server.id
  route_key          = "ANY /mcp"
  target             = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.mcp_server.id
}

# No authorizer — this is public OAuth metadata, fetched before login exists.
resource "aws_apigatewayv2_route" "mcp_discovery" {
  api_id    = aws_apigatewayv2_api.mcp_server.id
  route_key = "GET /.well-known/oauth-authorization-server"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
}

# Protected resource metadata (RFC 9728) — also public, fetched by MCP
# clients before the authorization-server metadata above, to learn which
# authorization server(s) protect this resource.
resource "aws_apigatewayv2_route" "mcp_protected_resource" {
  api_id    = aws_apigatewayv2_api.mcp_server.id
  route_key = "GET /.well-known/oauth-protected-resource"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
}

# Authorization endpoint proxy — also public (it sits in front of login). The
# Lambda logs the inbound query params, then 302s to Cognito's real
# /oauth2/authorize. Advertised as authorization_endpoint in the discovery
# document so the client's authorization request passes through our logs.
resource "aws_apigatewayv2_route" "mcp_authorize" {
  api_id    = aws_apigatewayv2_api.mcp_server.id
  route_key = "GET /oauth2/authorize"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
}

# Token endpoint proxy — same rationale as the authorize proxy above, for
# the token exchange/refresh step. Also public: it authenticates the caller
# itself (client secret or PKCE verifier in the forwarded request), same as
# Cognito's real /oauth2/token would.
resource "aws_apigatewayv2_route" "mcp_token" {
  api_id    = aws_apigatewayv2_api.mcp_server.id
  route_key = "POST /oauth2/token"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
}

# JWKS proxy — public keys, forwarded so this fetch is visible in our logs
# too. Advertised as jwks_uri in the discovery document.
resource "aws_apigatewayv2_route" "mcp_jwks" {
  api_id    = aws_apigatewayv2_api.mcp_server.id
  route_key = "GET /.well-known/jwks.json"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_server.id}"
}

resource "aws_apigatewayv2_stage" "mcp_server_default" {
  api_id      = aws_apigatewayv2_api.mcp_server.id
  name        = "$default"
  auto_deploy = true

  # Layer 1 — per-second rate limit on the tool-call route.
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  # Same cap on the unauthenticated discovery route — it has no auth in
  # front of it, so without an explicit throttle it falls back to the much
  # higher account-default limit.
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp_discovery.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  # Same cap on the protected resource metadata route — also unauthenticated.
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp_protected_resource.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  # Same cap on the authorize proxy route — also unauthenticated.
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp_authorize.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  # Same cap on the token proxy route — also unauthenticated (it authenticates
  # the caller itself via the forwarded request body/headers).
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp_token.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  # Same cap on the JWKS proxy route — also unauthenticated.
  route_settings {
    route_key              = aws_apigatewayv2_route.mcp_jwks.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.mcp_server_apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      sourceIp       = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      errorMessage   = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_cloudwatch_log_group" "mcp_server_apigw" {
  name              = "/aws/apigateway/serverless-mcp"
  retention_in_days = 14
}

resource "aws_lambda_permission" "mcp_server_api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_server.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.mcp_server.execution_arn}/*/*"
}
