resource "random_password" "postgres" {
  length  = 32
  special = false
}

# Static external-access token: GHA -> Backstage catalog refresh.
resource "random_password" "backstage_token" {
  length  = 48
  special = false
}
