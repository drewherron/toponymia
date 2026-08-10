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

# Inbound mail. **This zone is authoritative the moment the registrar's
# nameservers change**, so anything the old zone served and this one omits stops
# working then — silently, from our side, as bounces on the sender's.
#
# The apex MX is the case that matters: TERMS.md publishes support@toponymia.org
# as the DMCA designated agent and PRIVACY.md as the privacy contact, and SES
# only *sends*. Without these records a copyright notice we are obliged to
# receive would bounce. Nothing else in the pre-migration zone needed carrying
# over — it was a parking page.
resource "aws_route53_record" "mx" {
  count = length(var.mx_records) > 0 ? 1 : 0

  zone_id = local.zone_id
  name    = var.domain
  type    = "MX"
  ttl     = 3600
  records = var.mx_records
}

# One SPF record per domain — a second TXT starting `v=spf1` is a permerror
# rather than a merge — so every sender has to be listed here together. SES is
# not the only one: replies sent *from* the published contact address go out
# through the mail provider, and they fail SPF the day this record appears
# unless it is listed too. DMARC below is p=none so nothing would reject them,
# but a softfail still costs deliverability, and a DMCA reply landing in spam
# is its own kind of failure.
#
# Watch SPF's ten-lookup limit when adding to spf_includes: the default two cost
# three, because mailbox.org's own record contains an `mx` mechanism.
resource "aws_route53_record" "spf" {
  count = var.enable_ses ? 1 : 0

  zone_id = local.zone_id
  name    = var.domain
  type    = "TXT"
  ttl     = 600
  records = [
    "v=spf1 ${join(" ", [for host in var.spf_includes : "include:${host}"])} ~all"
  ]
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
