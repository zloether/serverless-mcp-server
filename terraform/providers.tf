provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      repo    = "serverless-mcp-server"
      project = var.project
    }
  }
}
