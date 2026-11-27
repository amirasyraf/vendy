locals {
  bucket_prefix = "cf-bucket-${local.account_id}-"
  bucket_arn    = "arn:aws:s3:::${local.bucket_prefix}*"
  bucket_tags   = merge(local.azure_tags, { "cf:purpose" = "buckets" })
}

variable "bucket_web_origin" {
  type    = string
  default = "https://idp.amirasyraf.dev"
}

resource "aws_iam_role" "bucket_upload" {
  name                 = "cf-bucket-upload"
  max_session_duration = 28800
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.idp.arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringLike                  = { "aws:RequestTag/vendy-bucket" = "${local.bucket_prefix}*" }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["vendy-bucket"] }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bucket_upload" {
  name = "write-selected-bucket"
  role = aws_iam_role.bucket_upload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["s3:PutObject", "s3:AbortMultipartUpload"]
      Resource  = "arn:aws:s3:::$${aws:PrincipalTag/vendy-bucket}/*"
      Condition = { StringEquals = { "aws:ResourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "idp_buckets" {
  name = "manage-buckets"
  role = aws_iam_role.idp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:CreateBucket", "s3:TagResource"]
        Resource = local.bucket_arn
        Condition = {
          StringEquals = {
            "aws:RequestTag/cf:poc"    = "true"
            "aws:RequestTag/cf:module" = "buckets"
          }
          Null = { "aws:RequestTag/cf:bucket-id" = "false" }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketTagging", "s3:PutBucketPublicAccessBlock", "s3:PutBucketCORS",
          "s3:PutBucketPolicy", "s3:PutLifecycleConfiguration", "s3:ListBucket",
        ]
        Resource  = local.bucket_arn
        Condition = { StringEquals = { "aws:ResourceAccount" = local.account_id } }
      },
      {
        Effect    = "Allow"
        Action    = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource  = "${local.bucket_arn}/*"
        Condition = { StringEquals = { "aws:ResourceAccount" = local.account_id } }
      },
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole", "sts:TagSession"]
        Resource = aws_iam_role.bucket_upload.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.azure_buckets.arn
      },
    ]
  })
}

provider "azuread" {
  tenant_id = var.azure_tenant_id
}

resource "azuread_application_registration" "buckets" {
  display_name     = "cf-idp-azure-buckets"
  sign_in_audience = "AzureADMyOrg"
}

resource "azuread_service_principal" "buckets" {
  client_id = azuread_application_registration.buckets.client_id
}

resource "azuread_application_password" "buckets" {
  application_id = azuread_application_registration.buckets.id
  display_name   = "Vendy POC Bucket service"
}

resource "azurerm_resource_group" "buckets" {
  name     = "cf-buckets"
  location = var.azure_location
  tags     = local.bucket_tags
}

resource "azurerm_storage_account" "buckets" {
  name                            = "cfbuckets${local.account_id}"
  resource_group_name             = azurerm_resource_group.buckets.name
  location                        = azurerm_resource_group.buckets.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  tags                            = local.bucket_tags

  blob_properties {
    cors_rule {
      allowed_origins    = [var.bucket_web_origin]
      allowed_methods    = ["PUT", "GET"]
      allowed_headers    = ["content-type", "if-match", "if-none-match", "x-ms-*"]
      exposed_headers    = ["ETag"]
      max_age_in_seconds = 300
    }
  }
}

resource "azurerm_role_assignment" "buckets" {
  scope                            = azurerm_storage_account.buckets.id
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = azuread_service_principal.buckets.object_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "aws_secretsmanager_secret" "azure_buckets" {
  name                    = "compute-factory/azure-buckets"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "azure_buckets" {
  secret_id = aws_secretsmanager_secret.azure_buckets.id
  secret_string = jsonencode({
    tenantId     = var.azure_tenant_id
    clientId     = azuread_application_registration.buckets.client_id
    clientSecret = azuread_application_password.buckets.value
  })
}

output "bucket_aws_upload_role_arn" {
  value = aws_iam_role.bucket_upload.arn
}

output "bucket_azure_account" {
  value = azurerm_storage_account.buckets.name
}

output "bucket_azure_secret_id" {
  value = aws_secretsmanager_secret.azure_buckets.name
}
