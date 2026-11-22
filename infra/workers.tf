# Containers worker pools (DESIGN.md §13.3-13.4). Every pool starts at 0 instances; the cluster
# autoscalers on the IDP host resize them. Workers join the k3s server on the IDP host over its EIP
# and report a provider ID the autoscaler of their cloud recognizes.

locals {
  aws_pools = {
    amd64 = { instance_type = "t3.micro", ami = data.aws_ssm_parameter.ubuntu_amd64.insecure_value }
    arm64 = { instance_type = "t4g.micro", ami = data.aws_ssm_parameter.ubuntu_arm64.insecure_value }
  }
  azure_pools = {
    amd64 = { size = "Standard_B2ats_v2", sku = "server" }
    arm64 = { size = "Standard_B2pts_v2", sku = "server-arm64" }
  }
  k3s_url = "https://${aws_eip.idp.public_ip}:6443"
}

data "aws_ssm_parameter" "ubuntu_amd64" {
  name = "/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "workers" {
  name        = "cf-container-workers"
  description = "Vendy container workers: WireGuard and kubelet"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "workers" {
  for_each = {
    wireguard = { protocol = "udp", port = 51820 }
    kubelet   = { protocol = "tcp", port = 10250 }
  }
  security_group_id = aws_security_group.workers.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "workers" {
  security_group_id = aws_security_group.workers.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_launch_template" "workers" {
  for_each      = local.aws_pools
  name          = "cf-workers-aws-${each.key}"
  image_id      = each.value.ami
  instance_type = each.value.instance_type
  key_name      = aws_key_pair.admin.key_name

  update_default_version = true

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.workers.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOF
    #!/bin/bash
    set -euxo pipefail
    token=$(curl -sfX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
    md() { curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1"; }
    id=$(md instance-id)
    zone=$(md placement/availability-zone)
    ip=$(md public-ipv4)
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='${var.k3s_version}' K3S_URL='${local.k3s_url}' \
      K3S_TOKEN='${random_password.k3s_token.result}' sh -s - agent --node-external-ip "$ip" \
      --kubelet-arg="provider-id=aws:///$zone/$id" --node-label vendy.io/pool=aws-${each.key}
  EOF
  )

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "cf-worker-aws-${each.key}"
      "cf:poc"    = "true"
      "cf:module" = "infra"
      "cf:pool"   = "aws-${each.key}"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      "cf:poc"    = "true"
      "cf:module" = "infra"
    }
  }
}

resource "aws_autoscaling_group" "workers" {
  for_each            = local.aws_pools
  name                = "cf-workers-aws-${each.key}"
  min_size            = 0
  max_size            = 2
  desired_capacity    = 0
  vpc_zone_identifier = data.aws_subnets.default.ids
  # Destroy terminates any worker the autoscaler started (./poc down).
  force_delete = true

  launch_template {
    id      = aws_launch_template.workers[each.key].id
    version = "$Latest"
  }

  # Scale-from-zero template for the autoscaler; the kubelet applies the real label.
  tag {
    key                 = "k8s.io/cluster-autoscaler/node-template/label/vendy.io/pool"
    value               = "aws-${each.key}"
    propagate_at_launch = false
  }

  tag {
    key                 = "cf:poc"
    value               = "true"
    propagate_at_launch = false
  }

  tag {
    key                 = "cf:module"
    value               = "infra"
    propagate_at_launch = false
  }

  lifecycle {
    # Owned by the cluster autoscaler once created.
    ignore_changes = [desired_capacity]
  }
}
