import os

# Dummy credentials so boto3 clients instantiate at module level without real AWS access.
# All API calls are mocked in individual tests.
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"

# Lambda env vars consumed at import time
os.environ["USAGE_TABLE_NAME"] = "usage-counters-test"
os.environ["DAILY_LIMIT"] = "200"
os.environ["MONTHLY_LIMIT"] = "2000"
os.environ["HOSTED_UI_DOMAIN"] = "https://serverless-mcp-test.auth.us-east-1.amazoncognito.com"
os.environ["COGNITO_ISSUER"] = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
os.environ["MCP_SERVER_URL"] = "https://abc123.execute-api.us-east-1.amazonaws.com/mcp"
os.environ["API_BASE_URL"] = "https://abc123.execute-api.us-east-1.amazonaws.com"
