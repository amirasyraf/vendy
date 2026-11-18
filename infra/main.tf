terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
  # Local state on purpose: nothing remote may outlive `./poc down`.
}

provider "aws" {
  region              = var.region
  profile             = var.aws_profile
  allowed_account_ids = ["134584031874"]
  default_tags {
    tags = {
      "cf:poc"    = "true"
      "cf:module" = "infra"
    }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  type    = string
  default = "amirasyraf"
}

variable "idp_instance_type" {
  type    = string
  default = "t4g.medium"
}

variable "admin_ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "cf-tfstate-${local.account_id}-apse2"
}
