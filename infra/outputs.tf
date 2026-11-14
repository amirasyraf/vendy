output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "idp_ip" {
  value = aws_eip.idp.public_ip
}

output "idp_instance_id" {
  value = aws_instance.idp.id
}

output "ssh_ca_secret_id" {
  value = aws_secretsmanager_secret.ssh_ca.name
}

output "ssh_ca_pub" {
  value = trimspace(tls_private_key.ssh_ca.public_key_openssh)
}

output "postgres_password" {
  value     = random_password.postgres.result
  sensitive = true
}

output "backstage_token" {
  value     = random_password.backstage_token.result
  sensitive = true
}
