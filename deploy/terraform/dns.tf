resource "aws_route53_zone" "main" {
  count = var.create_hosted_zone ? 1 : 0

  name = var.domain

  # Destroying a zone hands back its nameservers and they are not reissued, so
  # a rebuild means another registrar change and another propagation wait.
  lifecycle {
    prevent_destroy = true
  }
}

data "aws_route53_zone" "existing" {
  count = var.create_hosted_zone ? 0 : 1

  name         = "${var.domain}."
  private_zone = false
}

locals {
  zone_id = var.create_hosted_zone ? aws_route53_zone.main[0].zone_id : data.aws_route53_zone.existing[0].zone_id
}

resource "aws_route53_record" "apex" {
  zone_id = local.zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

# www resolves to the same box; Caddy is where the redirect to the apex lives,
# because a redirect needs to happen after TLS, not in DNS.
resource "aws_route53_record" "www" {
  zone_id = local.zone_id
  name    = "www.${var.domain}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

# ---------------------------------------------------------------------------
# SES
#
# This publishes the records that prove the domain is ours and that our mail is
# signed. It does NOT get the account out of the SES sandbox — that is a
# support request a human files, and until it is granted SES accepts mail only
# to addresses verified in the account. Since signup requires a verification
# code, a sandboxed account means nobody can register.
# ---------------------------------------------------------------------------

resource "aws_ses_domain_identity" "main" {
  count = var.enable_ses ? 1 : 0

  domain = var.domain
}

resource "aws_ses_domain_dkim" "main" {
  count = var.enable_ses ? 1 : 0

  domain = aws_ses_domain_identity.main[0].domain
}

resource "aws_route53_record" "ses_dkim" {
  count = var.enable_ses ? 3 : 0

  zone_id = local.zone_id
  name    = "${aws_ses_domain_dkim.main[0].dkim_tokens[count.index]}._domainkey.${var.domain}"
  type    = "CNAME"
  ttl     = 1800
  records = ["${aws_ses_domain_dkim.main[0].dkim_tokens[count.index]}.dkim.amazonses.com"]
}

# A custom MAIL FROM is what lets SPF align with the From: domain, which is
# what DMARC actually checks. Without it SES uses its own domain and the SPF
# pass belongs to Amazon rather than to us.
resource "aws_ses_domain_mail_from" "main" {
  count = var.enable_ses ? 1 : 0

  domain           = aws_ses_domain_identity.main[0].domain
  mail_from_domain = "mail.${var.domain}"

  # If the MX below ever disappears, fall back to SES's own domain rather than
  # refusing to send. Losing DMARC alignment is recoverable; losing every
  # signup verification code is not.
  behavior_on_mx_failure = "UseDefaultValue"
}

resource "aws_route53_record" "ses_mail_from_mx" {
  count = var.enable_ses ? 1 : 0

  zone_id = local.zone_id
  name    = aws_ses_domain_mail_from.main[0].mail_from_domain
  type    = "MX"
  ttl     = 600
  records = ["10 feedback-smtp.${var.region}.amazonses.com"]
}

resource "aws_route53_record" "ses_mail_from_spf" {
  count = var.enable_ses ? 1 : 0

  zone_id = local.zone_id
  name    = aws_ses_domain_mail_from.main[0].mail_from_domain
  type    = "TXT"
  ttl     = 600
  records = ["v=spf1 include:amazonses.com ~all"]
}

resource "aws_route53_record" "spf" {
  count = var.enable_ses ? 1 : 0

  zone_id = local.zone_id
  name    = var.domain
  type    = "TXT"
  ttl     = 600
  records = ["v=spf1 include:amazonses.com ~all"]
}

# p=none: monitor only, and with no rua= there is nowhere for reports to go
# either. This exists so the record is present and explicit rather than absent.
# Tightening to quarantine means first standing up a mailbox for rua and
# actually reading it — guessing at a policy before seeing one report is how
# legitimate mail gets dropped.
resource "aws_route53_record" "dmarc" {
  count = var.enable_ses ? 1 : 0

  zone_id = local.zone_id
  name    = "_dmarc.${var.domain}"
  type    = "TXT"
  ttl     = 600
  records = ["v=DMARC1; p=none;"]
}
