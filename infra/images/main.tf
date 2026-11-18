# Image tier: the SSH CA and the Azure gallery that golden images are published into. Images bake
# the CA public key, so the CA and the images live and die together. Separate local state from
# infra/ so `./poc down --keep-images` can keep this root while everything else is destroyed.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}

provider "aws" {
  region              = var.region
  profile             = var.aws_profile
  allowed_account_ids = ["134584031874"]
  default_tags {
    tags = {
      "cf:poc"    = "true"
      "cf:module" = "images"
    }
  }
}

provider "azurerm" {
  subscription_id = var.azure_subscription_id
  tenant_id       = var.azure_tenant_id
  features {}
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  type    = string
  default = "amirasyraf"
}

variable "azure_subscription_id" {
  type    = string
  default = "74b39a79-f79d-4adb-8ae0-fe9118fc564a"
}

variable "azure_tenant_id" {
  type    = string
  default = "c26545da-80fa-4c31-9a8f-a4ae475bd17e"
}

variable "azure_location" {
  type    = string
  default = "australiaeast"
}
