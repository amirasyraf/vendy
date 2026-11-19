# Mock SSH CA. Private key lives only in Secrets Manager (and local state). It is created with the
# image tier and replaced only when `./poc down` (without --keep-images) destroys that tier.
resource "tls_private_key" "ssh_ca" {
  algorithm = "ED25519"
}

resource "aws_secretsmanager_secret" "ssh_ca" {
  name                    = "compute-factory/ssh-ca"
  description             = "Vendy POC SSH user CA (OpenSSH private key)"
  recovery_window_in_days = 0 # delete immediately so the next `up` can reuse the name
}

resource "aws_secretsmanager_secret_version" "ssh_ca" {
  secret_id     = aws_secretsmanager_secret.ssh_ca.id
  secret_string = tls_private_key.ssh_ca.private_key_openssh
}

resource "aws_ssm_parameter" "ssh_ca_pub" {
  name  = "/compute-factory/ssh-ca-pub"
  type  = "String"
  value = trimspace(tls_private_key.ssh_ca.public_key_openssh)
}
