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

provider "azapi" {
  tenant_id       = var.azure_tenant_id
  subscription_id = var.azure_subscription_id
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

# AzureRM v4 probes the data plane using shared keys during account creation.
# Use Azure Resource Manager directly so shared-key access is never enabled.
resource "azapi_resource" "bucket_storage" {
  type      = "Microsoft.Storage/storageAccounts@2023-05-01"
  name      = "cfbuckets${local.account_id}"
  parent_id = azurerm_resource_group.buckets.id
  location  = azurerm_resource_group.buckets.location
  tags      = local.bucket_tags
  body = {
    kind = "StorageV2"
    sku  = { name = "Standard_LRS" }
    properties = {
      accessTier                   = "Hot"
      allowBlobPublicAccess        = false
      allowSharedKeyAccess         = false
      defaultToOAuthAuthentication = true
      minimumTlsVersion            = "TLS1_2"
      supportsHttpsTrafficOnly     = true
    }
  }
}

# The default blob service is created/deleted with its storage account. Only
# update its ARM properties; the child does not have a standalone delete API.
resource "azapi_update_resource" "bucket_blob_service" {
  type        = "Microsoft.Storage/storageAccounts/blobServices@2023-05-01"
  resource_id = "${azapi_resource.bucket_storage.id}/blobServices/default"
  body = {
    properties = {
      cors = {
        corsRules = [{
          allowedOrigins  = [var.bucket_web_origin]
          allowedMethods  = ["PUT", "GET"]
          allowedHeaders  = ["content-type", "if-match", "if-none-match", "x-ms-*"]
          exposedHeaders  = ["ETag"]
          maxAgeInSeconds = 300
        }]
      }
    }
  }
}

resource "azurerm_role_assignment" "buckets" {
  scope                            = azapi_resource.bucket_storage.id
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = azuread_service_principal.buckets.object_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
  depends_on                       = [azapi_update_resource.bucket_blob_service]
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
  value = azapi_resource.bucket_storage.name
}

output "bucket_azure_secret_id" {
  value = aws_secretsmanager_secret.azure_buckets.name
}
