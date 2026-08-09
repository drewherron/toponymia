data "aws_availability_zones" "available" {
  state = "available"
}

# A purpose-built VPC rather than the account default. One box does not need
# the isolation, but the default VPC is a black box you inherit rather than
# something you can read, and the whole point of doing this in AWS is to see
# the primitives.
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "toponymia" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "toponymia" }
}

# Single public subnet, single AZ. There is one instance and its state lives on
# its own EBS volume, so a second AZ would buy nothing that a restore from S3
# does not already cover.
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = { Name = "toponymia-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "toponymia-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "web" {
  name        = "toponymia-web"
  description = "Public HTTP/HTTPS for Caddy; everything else stays on localhost."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "toponymia-web" }
}

# Port 80 is not redundant with 443: Caddy needs it for the ACME HTTP-01
# challenge and for the redirect to HTTPS.
resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.web.id
  description       = "HTTP: ACME challenge and the redirect to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.web.id
  description       = "HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# Nothing here by default — see the ssh_ingress_cidrs variable. Postgres (5432),
# gunicorn (8000) and Redis (6379) are deliberately absent: all three bind to
# localhost, so there is no rule to write.
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = toset(var.ssh_ingress_cidrs)

  security_group_id = aws_security_group.web.id
  description       = "SSH fallback for a single operator IP"
  cidr_ipv4         = each.value
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

# Outbound has to be open: the box reaches Overpass, Photon, Let's Encrypt,
# SES, apt and S3.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.web.id
  description       = "All outbound"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
