#!/usr/bin/env bash
#
# Nightly database backup: dump, verify, upload, report.
#
#     sudo install -m 0755 deploy/box/backup.sh /usr/local/bin/toponymia-backup
#
# Run by toponymia-backup.timer. Reads the same /etc/toponymia/env the app
# does, plus two variables only this script needs:
#
#     TOPONYMIA_BACKUP_BUCKET=toponymia-backups-<account-id>   (tofu output)
#     AWS_DEFAULT_REGION=<region>
#
# No credentials: the instance profile supplies them.
#
# Every step that could fail quietly is checked. A backup job that stops
# working is the ordinary case, not the exotic one — it looks exactly like a
# backup job that is working, right up until the day you need it.

set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB is not set}"
: "${POSTGRES_USER:?POSTGRES_USER is not set}"
: "${TOPONYMIA_BACKUP_BUCKET:?TOPONYMIA_BACKUP_BUCKET is not set}"

export PGHOST="${POSTGRES_HOST:-localhost}"
export PGPORT="${POSTGRES_PORT:-5432}"
export PGDATABASE="$POSTGRES_DB"
export PGUSER="$POSTGRES_USER"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
workdir="$(mktemp -d)"
dump="$workdir/toponymia-$stamp.dump"
trap 'rm -rf "$workdir"' EXIT

# -Fc is the custom format: compressed already (so no gzip on top, whatever
# the sketch in the deployment notes says), and the only format pg_restore can
# restore selectively.
echo "dumping $PGDATABASE to $dump"
pg_dump --format=custom --file="$dump" "$PGDATABASE"

# A dump that exists is not a dump that restores. Reading the archive's table
# of contents proves the file is a complete, parseable custom-format archive
# rather than a truncated one — which is what a disk that filled mid-dump
# leaves behind, with pg_dump's exit code already spent.
echo "verifying archive"
if ! pg_restore --list "$dump" > "$workdir/toc"; then
    echo "FAILED: $dump is not a readable pg_dump archive" >&2
    exit 1
fi

# Naming a table the site cannot work without: an archive whose TOC parses but
# holds nothing is still not a backup. core_place is the anchor every article
# hangs off, so it is in every real dump of this database.
if ! grep -q 'TABLE DATA public core_place' "$workdir/toc"; then
    echo "FAILED: archive contains no core_place data" >&2
    exit 1
fi

size="$(stat -c %s "$dump")"
echo "archive ok: $size bytes, $(grep -c 'TABLE DATA' "$workdir/toc") tables"

key="dumps/toponymia-$stamp.dump"
echo "uploading s3://$TOPONYMIA_BACKUP_BUCKET/$key"
aws s3 cp "$dump" "s3://$TOPONYMIA_BACKUP_BUCKET/$key"

# The heartbeat. The alarm on this metric treats missing data as breaching, so
# what pages is the *absence* of a success — which is the only signal that
# catches the machine being off, the timer being disabled, or this script
# never having been installed. A failure metric would need the script to run
# in order to report that the script isn't running.
#
# Last, and only on success: everything above must have worked to reach it.
if aws cloudwatch put-metric-data \
    --namespace Toponymia \
    --metric-name BackupSucceeded \
    --value 1 \
    --unit Count 2>/dev/null; then
    echo "published BackupSucceeded"
else
    # Worth finishing green: the dump is safely in S3, which is the job. But
    # say so, because from here on the alarm will claim otherwise.
    echo "WARNING: dump uploaded but the success metric did not publish" >&2
fi

echo "backup complete: $key"
