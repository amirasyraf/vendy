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

# Terraform owns the secret container and read policy, not the value. `poc up`
# creates the credential and writes the value without involving Terraform state.
resource "aws_secretsmanager_secret" "azure_reader" {
  name                    = "compute-factory/azure-reader"
  description             = "Vendy IDP read-only Azure credential (service principal certificate)"
  recovery_window_in_days = 0
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
