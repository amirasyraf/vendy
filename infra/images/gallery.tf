# Golden-image gallery, filled by the builder repo's Packer bake.
locals {
  azure_tags = { "cf:poc" = "true", "cf:module" = "images" }
}

resource "azurerm_resource_group" "images" {
  name     = "cf-images"
  location = var.azure_location
  tags     = local.azure_tags
}

resource "azurerm_shared_image_gallery" "images" {
  name                = "cfimages"
  resource_group_name = azurerm_resource_group.images.name
  location            = azurerm_resource_group.images.location
  description         = "Compute Factory golden images (POC)"
  tags                = local.azure_tags
}

# One definition per OS; Packer publishes versions, Terraform launches the latest.
resource "azurerm_shared_image" "ubuntu" {
  name                = "ubuntu"
  gallery_name        = azurerm_shared_image_gallery.images.name
  resource_group_name = azurerm_resource_group.images.name
  location            = azurerm_resource_group.images.location
  os_type             = "Linux"
  hyper_v_generation  = "V2"
  architecture        = "x64"
  # The sizes with quota in australiaeast (v6/v7) are NVMe-only; the Canonical base supports both.
  disk_controller_type_nvme_enabled = true
  identifier {
    publisher = "compute-factory"
    offer     = "ubuntu"
    sku       = "26.04"
  }
  tags = local.azure_tags
}
