# On-box configuration

The configuration the box needs, kept in the repo so it is reviewed and
versioned rather than typed into a live server at 1 a.m.
[`../terraform/`](../terraform/README.md) creates the infrastructure; these
configure what runs on it.

| File | Goes to |
|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| `toponymia.service` | `/etc/systemd/system/toponymia.service` |
| `backup.sh` | `/usr/local/bin/toponymia-backup` |
| `toponymia-backup.service` | `/etc/systemd/system/toponymia-backup.service` |
| `toponymia-backup.timer` | `/etc/systemd/system/toponymia-backup.timer` |
| `amazon-cloudwatch-agent.json` | `/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json` |

```sh
sudo install -m 0644 deploy/box/Caddyfile /etc/caddy/Caddyfile
sudo install -m 0644 deploy/box/toponymia.service \
    /etc/systemd/system/toponymia.service
sudo install -m 0755 deploy/box/backup.sh /usr/local/bin/toponymia-backup
sudo install -m 0644 deploy/box/toponymia-backup.service \
    /etc/systemd/system/toponymia-backup.service
sudo install -m 0644 deploy/box/toponymia-backup.timer \
    /etc/systemd/system/toponymia-backup.timer
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo install -m 0644 deploy/box/amazon-cloudwatch-agent.json \
    /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now toponymia caddy toponymia-backup.timer
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
```

The backup script needs two variables in `/etc/toponymia/env` that the app
itself doesn't use: `TOPONYMIA_BACKUP_BUCKET` (the `backup_bucket` output from
`tofu apply`) and `AWS_DEFAULT_REGION`. It needs no credentials — the instance
profile supplies them.

Run it once by hand before trusting the timer:

```sh
sudo systemctl start toponymia-backup && journalctl -u toponymia-backup -n 30
```

Neither file contains a secret. Everything sensitive — `DJANGO_SECRET_KEY`, the
database password, the SES SMTP credentials — belongs in a root-owned `0600`
environment file at `/etc/toponymia/env`, which the systemd unit loads and
which must never be committed.

## Before this starts

**`Caddyfile` has one placeholder and will not parse until you replace it.**
`REPLACE_WITH_ADMIN_IP/32` is the address allowed to reach Django's admin;
everyone else gets a 403. `127.0.0.1/32` is already allowed alongside it, so
deleting the placeholder entirely is a valid choice — it leaves the admin
reachable only through an SSM port-forward, which is the stricter option.

The failure mode is deliberate. A placeholder that parses would leave the admin
open to the internet and look exactly like a configured system; a placeholder
that doesn't parse is a service that refuses to start.

## The CloudWatch agent config, which JSON can't explain itself

Every other file here carries its reasoning in comments. JSON has none, so the
parts of `amazon-cloudwatch-agent.json` that are load-bearing rather than
default live here.

**The agent is the half that makes the alarms real.** `../terraform/logs.tf`
creates three log groups, a metric filter and four alarms, and until this config
runs, two of them watch nothing. That is not a quiet degradation:
`aws_cloudwatch_metric_alarm.disk` sets `treat_missing_data = "breaching"` on
purpose, so a dead or misconfigured agent alarms. Get a name wrong here and you
get a permanent false alarm about a healthy disk — which trains you to ignore
the one alert that means the database is about to stop.

**`namespace` must stay `Toponymia`.** The agent's own default is `CWAgent`, and
both the disk alarm and `backup.sh`'s `BackupSucceeded` metric look in
`Toponymia`. Three places, one string.

**`aggregation_dimensions` is why the disk alarm resolves.** The alarm keys on
exactly `InstanceId` + `path`. Left alone, the agent tags disk metrics with
extra dimensions, and a metric carrying more dimensions than the alarm asks for
is a different metric as far as CloudWatch is concerned — the alarm sits at
`INSUFFICIENT_DATA` beside a metric that plainly has data. `drop_device` and the
explicit rollup are what make the two line up.

**`retention_in_days: -1` means "don't touch it".** Retention is set in
`logs.tf` because these logs hold client IP addresses and the window is
published in `PRIVACY.md` §2. Any positive number here would let the agent
overwrite that on every restart, quietly making the policy untrue.

**`DJANGO_LOG_DIR` stops being optional.** The agent tails files; it does not
read the journal. Gunicorn's stderr — where Django writes unhandled 500s, and
where the `ERROR django.request` metric filter expects to find them — reaches
CloudWatch only via `/var/log/toponymia/toponymia.log`, which `settings.py`
writes only when `DJANGO_LOG_DIR` is set. Leave it unset and the 500 alarm can
never fire. `toponymia.service` already creates the directory.

**`run_as_user: root`.** The three logs are owned by three different service
users and none are world-readable, so the packaged `cwagent` user can read none
of them. Root is the honest choice over loosening the permissions on files that
contain personal data.

**The Postgres glob** matches the version number Ubuntu puts in the filename
(`postgresql-16-main.log`), so a major-version upgrade doesn't silently end log
shipping.

## Two numbers that are promises, not preferences

- **30 days of access logs.** `PRIVACY.md` §2 publishes that window, so the
  `roll_keep_for 720h` in the `Caddyfile` is what makes the policy true. It is
  the same commitment as `log_retention_days` on the CloudWatch side; change
  one and you have to change the other and the policy.
- **`--workers 3`.** Without `DJANGO_REDIS_URL` set, every rate limit in
  `settings.py` is counted per worker, so the effective limit is three times
  what the code says. Either set the Redis URL or know which multiplier you
  shipped — but don't edit the rates to compensate, because that hides the
  cause and silently over-throttles the day Redis does get added.

## What is deliberately not here

**HSTS is set by Caddy, not Django.** `settings.py` leaves
`SECURE_HSTS_SECONDS` unset so the proxy owns the header — a dev box then can't
emit it and pin a developer's browser to HTTPS on localhost.

**No `preload` on HSTS.** Getting a domain onto the preload list is close to
irreversible and deserves a decision of its own.

**The proxy count is not in these files.** `DJANGO_TRUSTED_PROXY_COUNT` lives in
the environment file and must equal the number of proxies actually in front of
Django — `1` for the Caddy here. It is a security control: unset, DRF treats the
whole client-supplied `X-Forwarded-For` as the throttle identity, so rotating
that header defeats every rate limit including the one protecting the Overpass
budget. Putting anything else in front (CloudFront, an ALB) makes it `2`.

**Anything that decides.** `backup.sh` takes and verifies a dump; restoring is
a judgement call and stays a documented procedure rather than a script that
could run by accident.

**EBS snapshots.** `pg_dump` covers the data. Nothing here covers the box
itself — the packages, the env file, this config — so a lost instance is still
a rebuild rather than a restore.
