terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    azuread = { source = "hashicorp/azuread", version = "~> 3.0" }
    azapi   = { source = "Azure/azapi", version = "~> 2.0" }
  }
  # Local state on purpose: nothing remote may outlive `./poc down`. The SSH CA and image gallery
  # are in the separate images/ root, which `./poc down --keep-images` keeps.
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
  type = string
  # 8 GiB: Backstage and Postgres plus the k3s control plane for Containers (DESIGN.md §13.3).
  default = "t4g.large"
}

# Containers cluster versions. The autoscaler's minor version must match k3s's.
variable "k3s_version" {
  type    = string
  default = "v1.37.2+k3s1"
}

variable "cluster_autoscaler_version" {
  type    = string
  default = "v1.37.0"
}

variable "admin_ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  state_bucket  = "cf-tfstate-${local.account_id}-apse2"
  ssh_ca_secret = "compute-factory/ssh-ca" # created by infra/images
}
