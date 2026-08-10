# On-box configuration

The two service files the box needs, kept in the repo so they are reviewed and
versioned rather than typed into a live server at 1 a.m.
[`../terraform/`](../terraform/README.md) creates the infrastructure; these
configure what runs on it.

| File | Goes to |
|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| `toponymia.service` | `/etc/systemd/system/toponymia.service` |

```sh
sudo install -m 0644 deploy/box/Caddyfile /etc/caddy/Caddyfile
sudo install -m 0644 deploy/box/toponymia.service \
    /etc/systemd/system/toponymia.service
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now toponymia caddy
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

**Backups.** The nightly `pg_dump` timer that writes to the S3 bucket isn't
written yet.
