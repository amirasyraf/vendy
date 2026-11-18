output "ssh_ca_secret_id" {
  value = aws_secretsmanager_secret.ssh_ca.name
}

output "ssh_ca_pub" {
  value = trimspace(tls_private_key.ssh_ca.public_key_openssh)
}

# OpenSSH format (SHA256:<base64>); every golden image is tagged cf:ssh-ca-fingerprint with it.
output "ssh_ca_fingerprint" {
  value = tls_private_key.ssh_ca.public_key_fingerprint_sha256
}

output "azure_gallery" {
  value = "${azurerm_resource_group.images.name}/${azurerm_shared_image_gallery.images.name}"
}
