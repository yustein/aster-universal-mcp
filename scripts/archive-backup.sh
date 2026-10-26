#!/usr/bin/env bash
# Produce a private local backup; offsite upload/retention is separately configured.
set -euo pipefail
umask 077

archive_repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
archive_deploy_dir="$archive_repo_dir/deploy/archive"
archive_destination=${1:?Usage: scripts/archive-backup.sh /absolute/backup-directory}
case "$archive_destination" in /*) ;; *) printf '%s\n' 'Backup directory must be absolute' >&2; exit 2 ;; esac
mkdir -p -- "$archive_destination"
archive_partial=$(mktemp -d "$archive_destination/.aster-archive-backup.XXXXXXXX")
archive_resume=()
archive_compose=(docker compose --project-directory "$archive_deploy_dir" --env-file "$archive_deploy_dir/.env" -f "$archive_deploy_dir/compose.yaml")

archive_finish() {
    archive_result=$?
    trap - EXIT
    if ((${#archive_resume[@]})); then
        "${archive_compose[@]}" start "${archive_resume[@]}" >&2 || archive_result=1
    fi
    if ((archive_result)); then
        printf 'Backup failed; private partial output remains at %s\n' "$archive_partial" >&2
    fi
    exit "$archive_result"
}
trap archive_finish EXIT

archive_running=$("${archive_compose[@]}" ps --services --status running)
for archive_service in watch worker; do
    if [[ $'\n'"$archive_running"$'\n' == *$'\n'"$archive_service"$'\n'* ]]; then
        archive_resume+=("$archive_service")
    fi
done
if ((${#archive_resume[@]})); then
    "${archive_compose[@]}" stop --timeout 120 "${archive_resume[@]}" >&2
fi

"${archive_compose[@]}" exec -T db pg_dump -U postgres -d aster_archive \
    --format=custom --no-owner --no-acl > "$archive_partial/database.dump"
"${archive_compose[@]}" run --rm --no-deps -T backup > "$archive_partial/files.tar.gz"
cp -- "$archive_deploy_dir/.env" "$archive_partial/deployment.env"
chmod 600 "$archive_partial/"*
(
    cd -- "$archive_partial"
    sha256sum database.dump files.tar.gz deployment.env > SHA256SUMS
)
archive_name="aster-archive-$(date -u +%Y%m%dT%H%M%SZ)-${archive_partial##*.}"
archive_complete="$archive_destination/$archive_name"
mv -T -- "$archive_partial" "$archive_complete"
printf 'Completed local backup: %s\n' "$archive_complete"
