# Bucket names are globally unique, so the account ID keeps this from colliding
# with someone else's guess at "toponymia-backups".
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backups" {
  bucket = "toponymia-backups-${data.aws_caller_identity.current.account_id}"

  tags = { Name = "toponymia-backups" }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning is what makes the write-only instance policy in iam.tf worth
# something: even an overwrite with garbage leaves the previous dump behind.
resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-old-dumps"
    status = "Enabled"

    filter {}

    expiration {
      days = var.backup_retention_days
    }

    # Without this, versioning above would quietly keep every superseded dump
    # forever and the expiration rule would only ever hide them.
    noncurrent_version_expiration {
      noncurrent_days = var.backup_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}
