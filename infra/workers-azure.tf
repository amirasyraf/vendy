# Azure container worker pools (DESIGN.md §13.3-13.4): Uniform scale sets at 0 instances in their
# own resource group. The Azure autoscaler on the IDP host uses cf-idp-azure-reader, which is
# Contributor on this group only (and Reader elsewhere).

provider "azurerm" {
  subscription_id = var.azure_subscription_id
  tenant_id       = var.azure_tenant_id
  features {}
}

# Object ID of the cf-idp-azure-reader service principal (persistent identity, DESIGN.md §9).
variable "azure_reader_object_id" {
  type    = string
  default = "9cf00a7f-417f-4f28-a71d-333a03f17511"
}

locals {
  azure_tags = { "cf:poc" = "true", "cf:module" = "infra" }
}

resource "azurerm_resource_group" "workers" {
  name     = "cf-workers"
  location = var.azure_location
  tags     = local.azure_tags
}

resource "azurerm_role_assignment" "autoscaler" {
  scope                = azurerm_resource_group.workers.id
  role_definition_name = "Contributor"
  principal_id         = var.azure_reader_object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_virtual_network" "workers" {
  name                = "cf-workers"
  location            = azurerm_resource_group.workers.location
  resource_group_name = azurerm_resource_group.workers.name
  address_space       = ["10.70.0.0/16"]
  tags                = local.azure_tags
}

resource "azurerm_subnet" "workers" {
  name                 = "workers"
  resource_group_name  = azurerm_resource_group.workers.name
  virtual_network_name = azurerm_virtual_network.workers.name
  address_prefixes     = ["10.70.0.0/24"]
}

resource "azurerm_network_security_group" "workers" {
  name                = "cf-workers"
  location            = azurerm_resource_group.workers.location
  resource_group_name = azurerm_resource_group.workers.name
  tags                = local.azure_tags

  security_rule {
    name                       = "wireguard"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Udp"
    source_port_range          = "*"
    destination_port_range     = "51820"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "kubelet"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "10250"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "workers" {
  subnet_id                 = azurerm_subnet.workers.id
  network_security_group_id = azurerm_network_security_group.workers.id
}

resource "azurerm_linux_virtual_machine_scale_set" "workers" {
  for_each            = local.azure_pools
  name                = "cf-workers-azure-${each.key}"
  resource_group_name = azurerm_resource_group.workers.name
  location            = azurerm_resource_group.workers.location
  sku                 = each.value.size
  instances           = 0
  admin_username      = "azureuser"
  upgrade_mode        = "Manual"
  # The autoscaler expects exactly the instances it asked for.
  overprovision = false

  admin_ssh_key {
    username   = "azureuser"
    public_key = trimspace(file(pathexpand(var.admin_ssh_public_key_path)))
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = each.value.sku
    version   = "latest"
  }

  os_disk {
    storage_account_type = "Standard_LRS"
    caching              = "ReadWrite"
  }

  network_interface {
    name    = "nic"
    primary = true
    ip_configuration {
      name      = "ip"
      primary   = true
      subnet_id = azurerm_subnet.workers.id
      # New VNets have no default outbound access; a public IP per instance also carries WireGuard.
      public_ip_address {
        name = "pip"
      }
    }
  }

  custom_data = base64encode(<<-EOF
    #!/bin/bash
    set -euxo pipefail
    rid=$(curl -sf -H Metadata:true "http://169.254.169.254/metadata/instance/compute/resourceId?api-version=2021-02-01&format=text")
    # IMDS does not report Standard SKU public IPs; the instance's outbound address is its public IP.
    ip=""
    for _ in $(seq 1 30); do
      ip=$(curl -sf --max-time 5 https://api.ipify.org || true)
      [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && break
      sleep 2
    done
    [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]]
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='${var.k3s_version}' K3S_URL='${local.k3s_url}' \
      K3S_TOKEN='${random_password.k3s_token.result}' sh -s - agent --node-external-ip "$ip" \
      --kubelet-arg="provider-id=azure://$rid" --node-label vendy.io/pool=azure-${each.key}
  EOF
  )

  tags = merge(local.azure_tags, {
    # Scale-from-zero template label (vendy.io/pool; Azure tag names cannot contain "/").
    "k8s.io_cluster-autoscaler_node-template_label_vendy.io_pool" = "azure-${each.key}"
  })

  depends_on = [azurerm_subnet_network_security_group_association.workers]

  lifecycle {
    # Owned by the cluster autoscaler once created.
    ignore_changes = [instances]
  }
}
