variable "region" {
  description = "AWS region. Must be one where SES is available, since the site's outbound mail lives in the same account."
  type        = string
  default     = "us-east-1"
}

variable "domain" {
  description = "Apex domain. The hosted zone, the A records and the SES identity are all derived from it."
  type        = string
  default     = "toponymia.org"
}

variable "instance_type" {
  description = "Graviton/arm64 instance. The AMI filter below is arm64-only, so a non-t4g/m*g type will fail to launch."
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  description = "Root EBS size. Postgres, the revision history and the logs all share this volume, so the disk alarm in CloudWatch is the one that matters."
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = "Optional SSH public key material. Leave empty to manage the box entirely over SSM Session Manager, which is the default posture here."
  type        = string
  default     = ""
}

variable "ssh_ingress_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach port 22. Empty by default and meant to stay that
    way: the instance profile grants SSM Session Manager access, which is
    IAM-gated and logged, so there is no need to expose 22 to anything. Set it
    to ["your.ip.here/32"] only as a fallback, never to 0.0.0.0/0.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.ssh_ingress_cidrs, "0.0.0.0/0")
    error_message = "Refusing to open SSH to the whole internet. Use SSM, or a /32."
  }
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch log retention. This is not a preference: PRIVACY.md publishes a
    30-day window for access logs, so raising it makes the site's own privacy
    policy untrue. Lowering it is fine.
  EOT
  type        = number
  default     = 30
}

variable "backup_retention_days" {
  description = "How long nightly pg_dumps live in S3 before the lifecycle rule expires them. Unrelated to log retention above — backups are not access logs."
  type        = number
  default     = 30
}

variable "alert_email" {
  description = "Address that receives CloudWatch alarms and the billing alert. Leave empty and the alarms still fire, but silently — which is the failure mode they exist to prevent. AWS sends a confirmation mail to this address that has to be clicked before anything is delivered."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Budget threshold for the cost alert. The expected all-in bill is roughly $17-19/mo, so this leaves headroom for a mistake without waiting for a month-end surprise."
  type        = number
  default     = 30
}

variable "create_hosted_zone" {
  description = "Create the Route 53 zone. Set false if the zone already exists in the account and should be looked up instead — recreating a zone changes its nameservers and breaks DNS until the registrar is updated again."
  type        = bool
  default     = true
}

variable "enable_ses" {
  description = <<-EOT
    Create the SES domain identity and publish its DKIM/SPF/DMARC records.
    Verification of the domain is automatic once the records resolve, but
    leaving the SES *sandbox* is a manual support request that only a human can
    file — this flag does not do that half.
  EOT
  type        = bool
  default     = true
}
