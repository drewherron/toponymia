# Infrastructure

OpenTofu (or Terraform — the config uses no tool-specific syntax) for the
single box that serves toponymia.org.

```sh
tofu init
cp terraform.tfvars.example terraform.tfvars   # then edit
tofu plan
tofu apply
```

State is local and gitignored. One operator, one box; an S3 backend is the
right answer only once more than one person can run `apply`.

## What this creates

A VPC with one public subnet and an internet gateway; a security group open on
80 and 443 only; a `t4g.small` (arm64 Ubuntu 24.04) with an encrypted `gp3`
root volume and IMDSv2 required; an Elastic IP; a Route 53 zone with A records
for the apex and `www`; SES domain identity with DKIM, SPF and a MAIL FROM
subdomain; a private, versioned, lifecycle-expiring S3 bucket for `pg_dump`
backups; an instance profile scoped to that bucket and this project's log
groups; CloudWatch log groups, a metric filter and alarms; and a monthly
budget alert.

## What this deliberately does not do

**Configure the box.** No user-data, no cloud-init. Installing Postgres, Caddy,
gunicorn and the app is a separate step, and a half-automated bootstrap is
worse than either a fully automated one or an honest manual run — you end up
unsure which half ran. Secrets are the other reason: user-data is readable
through the metadata service and the console, so `DJANGO_SECRET_KEY` and the
database password cannot live there. They belong in a root-owned `0600` env
file on the box.

**Leave the SES sandbox.** The DKIM and SPF records here prove the domain is
ours, which is the automatable half. Production access is a support request a
human files, it can take about a day, and until it is granted SES only delivers
to addresses separately verified in the account. Signup requires an emailed
verification code, so a sandboxed account means *nobody can register, including
you*. Start that request early and confirm it before pointing DNS.

## Order of operations

1. `tofu apply`.
2. Take the `nameservers` output to the domain registrar. This is the one step
   that cannot be scripted, and nothing resolves until it propagates.
3. Confirm the SES domain identity has verified (the DKIM records above do it
   automatically once DNS resolves), and that production access has been
   granted.
4. Bootstrap the box, then test over `curl --resolve toponymia.org:443:<EIP>`
   *before* relying on DNS — secure cookies need real HTTPS, so that is the
   first honest login test.
5. Click the confirmation link AWS mails to `alert_email`, or the alarms and
   the budget alert deliver nothing.

## Couplings worth knowing

- **`log_retention_days` is a published promise.** PRIVACY.md commits to a
  30-day window for access logs. Raising the variable makes the site's own
  privacy policy false.
- **The CloudWatch agent must publish to the `Toponymia` namespace.** The
  instance policy conditions `cloudwatch:PutMetricData` on it, and the disk
  alarm reads it. An agent left on the default `CWAgent` namespace will be
  denied, and the disk alarm — which treats missing data as breaching — will
  fire instead of going quiet.
- **The gunicorn log group is where 500s are detected.** Django writes
  unhandled exceptions to stderr unconditionally; the metric filter looks for
  `ERROR django.request` there. Ship gunicorn's error log to
  `/toponymia/gunicorn` or the alarm watches an empty group forever.
- **The instance can write backups but not delete them.** Expiry is the
  bucket's lifecycle rule. If a retention change seems not to work, change it
  here rather than granting the box `s3:DeleteObject`.
- **`ignore_changes = [ami]`.** Canonical publishes new AMIs constantly and the
  data source follows them, so without this a routine plan proposes replacing
  the instance — and the database with it. Moving to a new AMI means building a
  box and restoring, not letting `apply` do it by surprise.
