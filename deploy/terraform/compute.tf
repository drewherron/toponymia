data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-arm64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "operator" {
  count = var.ssh_public_key == "" ? 0 : 1

  key_name   = "toponymia-operator"
  public_key = var.ssh_public_key
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  key_name               = var.ssh_public_key == "" ? null : aws_key_pair.operator[0].key_name

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true # One flag, no cost, and it covers the database at rest.

    tags = { Name = "toponymia-root" }
  }

  # IMDSv2 required. This is the control that closes the classic
  # SSRF-reads-instance-credentials path, and the instance profile below is
  # exactly what such a request would be after.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  lifecycle {
    # Canonical publishes new Ubuntu AMIs constantly and most_recent above
    # would follow them, so without this a routine plan proposes replacing the
    # instance — taking the database with it. Deliberate AMI moves mean
    # building a new box and restoring, not letting apply do it by surprise.
    ignore_changes = [ami]
  }

  tags = { Name = "toponymia" }
}

# The public IP has to outlive stop/start, or the A records point at nothing
# after the first reboot that reschedules the instance.
resource "aws_eip" "app" {
  domain = "vpc"

  tags = { Name = "toponymia" }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}
