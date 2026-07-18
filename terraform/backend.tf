terraform {
  backend "s3" {
    bucket       = "CHANGE-ME-serverless-mcp-server-tf-state-<account-id>-<region>"
    key          = "tf/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
