# IDP host: single EC2 running Caddy + Backstage + Postgres via Docker Compose (deployed by compute-factory-idp/deploy/deploy.sh).
data "aws_ssm_parameter" "ubuntu_arm64" {
  name = "/aws/service/canonical/ubuntu/server/26.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "admin" {
  key_name   = "cf-idp-admin"
  public_key = trimspace(file(pathexpand(var.admin_ssh_public_key_path)))
}

resource "aws_security_group" "idp" {
  name        = "cf-idp"
  description = "Compute Factory IDP: SSH + HTTP(S)"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "idp" {
  for_each          = toset(["22", "80", "443"])
  security_group_id = aws_security_group.idp.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
}

resource "aws_vpc_security_group_egress_rule" "idp" {
  security_group_id = aws_security_group.idp.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_iam_role" "idp" {
  name = "cf-idp"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "idp_read_ca" {
  name = "read-ssh-ca"
  role = aws_iam_role.idp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.ssh_ca.arn
    }]
  })
}

# The VM request form and VM pages show instance-type specs (vCPU, memory, arch) live from AWS.
# Read-only; DescribeInstanceTypes does not support resource-level scoping.
resource "aws_iam_role_policy" "idp_describe_instance_types" {
  name = "describe-instance-types"
  role = aws_iam_role.idp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ec2:DescribeInstanceTypes"
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "idp_ssm" {
  role       = aws_iam_role.idp.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "idp" {
  name = "cf-idp"
  role = aws_iam_role.idp.name
}

resource "aws_instance" "idp" {
  ami                    = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  instance_type          = var.idp_instance_type
  key_name               = aws_key_pair.admin.key_name
  vpc_security_group_ids = [aws_security_group.idp.id]
  iam_instance_profile   = aws_iam_instance_profile.idp.name

  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # containers reach IMDS for the instance role
  }

  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y docker.io docker-compose-v2
    systemctl enable --now docker
    usermod -aG docker ubuntu
    install -d -o ubuntu -g ubuntu /opt/idp
    touch /var/lib/cloud/instance/cf-idp-ready
  EOF

  tags = { Name = "cf-idp" }

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_eip" "idp" {
  domain   = "vpc"
  instance = aws_instance.idp.id
  tags     = { Name = "cf-idp" }
}
