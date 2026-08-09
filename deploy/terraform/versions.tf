terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State stays local: one operator, one box. An S3 + DynamoDB backend is the
  # right answer only once more than one person can run apply, and it adds two
  # more resources to bootstrap before anything else can exist.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "toponymia"
      ManagedBy = "opentofu"
    }
  }
}
