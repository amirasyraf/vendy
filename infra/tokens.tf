resource "random_password" "postgres" {
  length  = 32
  special = false
}

# Static external-access token: GHA -> Backstage catalog refresh.
resource "random_password" "backstage_token" {
  length  = 48
  special = false
}

# k3s join token: the server and every worker (launch template / scale set custom data).
resource "random_password" "k3s_token" {
  length  = 48
  special = false
}
