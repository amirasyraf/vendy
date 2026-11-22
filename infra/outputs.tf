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
  value = local.ssh_ca_secret
}

output "azure_reader_secret_id" {
  value = aws_secretsmanager_secret.azure_reader.name
}

output "postgres_password" {
  value     = random_password.postgres.result
  sensitive = true
}

output "backstage_token" {
  value     = random_password.backstage_token.result
  sensitive = true
}

# Containers cluster (consumed by pocctl/idp.py -> the IDP .env -> deploy/cluster-install.sh).
output "k3s_version" {
  value = var.k3s_version
}

output "cluster_autoscaler_version" {
  value = var.cluster_autoscaler_version
}

output "k3s_token" {
  value     = random_password.k3s_token.result
  sensitive = true
}

output "aws_worker_groups" {
  value = join(",", [for pool in sort(keys(local.aws_pools)) : aws_autoscaling_group.workers[pool].name])
}

output "azure_workers_rg" {
  value = azurerm_resource_group.workers.name
}

output "azure_worker_scale_sets" {
  value = join(",", [for pool in sort(keys(local.azure_pools)) : azurerm_linux_virtual_machine_scale_set.workers[pool].name])
}
