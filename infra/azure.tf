# The IDP's read-only Azure credential. The golden-image gallery is in images/; identities (GitHub
# OIDC app, Reader SP) live outside up/down; see DESIGN.md §9.
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

# Reader service principal cf-idp-azure-reader; its certificate (PEM key + cert) is kept locally.
variable "azure_reader_client_id" {
  type    = string
  default = "1a334f6b-9420-4747-8336-43aff14edb93"
}

variable "azure_reader_cert_path" {
  type    = string
  default = "../.poc-azure-reader.pem"
}

resource "aws_secretsmanager_secret" "azure_reader" {
  name                    = "compute-factory/azure-reader"
  description             = "Vendy IDP read-only Azure credential (service principal certificate)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "azure_reader" {
  secret_id = aws_secretsmanager_secret.azure_reader.id
  secret_string = jsonencode({
    tenantId       = var.azure_tenant_id
    clientId       = var.azure_reader_client_id
    subscriptionId = var.azure_subscription_id
    location       = var.azure_location
    certificatePem = file("${path.module}/${var.azure_reader_cert_path}")
  })
}

resource "aws_iam_role_policy" "idp_read_azure_reader" {
  name = "read-azure-reader"
  role = aws_iam_role.idp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.azure_reader.arn
    }]
  })
}
