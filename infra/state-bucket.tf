# Holds per-VM Terraform state (vms/<platform>/<name>/terraform.tfstate). Emptied and deleted by `./poc down`.
resource "aws_s3_bucket" "tfstate" {
  bucket        = local.state_bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
