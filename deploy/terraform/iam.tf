data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "toponymia-instance"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_instance_profile" "app" {
  name = "toponymia-instance"
  role = aws_iam_role.app.name
}

# Session Manager instead of an open port 22: access becomes IAM-gated and
# logged, and the security group needs no inbound SSH rule at all. The SSM
# agent ships on the Ubuntu AMI.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "app" {
  # Backups: write-and-list only. The box never needs to delete a dump — the
  # bucket's lifecycle rule does that — so a compromised instance cannot erase
  # the backups it just wrote.
  statement {
    sid       = "WriteBackups"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backups.arn}/*"]
  }

  statement {
    sid       = "ListBackupBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backups.arn]
  }

  # Log shipping, scoped to this project's groups rather than the account's.
  statement {
    sid = "ShipLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [for group in aws_cloudwatch_log_group.box : "${group.arn}:*"]
  }

  # PutMetricData takes no resource ARN — the API has no per-metric resource to
  # name — so this one cannot be narrowed further. The condition limits it to
  # the agent's own namespace, which is as close as the API allows.
  statement {
    sid       = "PublishAgentMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Toponymia"]
    }
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "toponymia-instance"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}
