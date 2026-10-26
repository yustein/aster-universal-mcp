# Private Drive export intake

`scripts/archive-drive-intake.py` checks one explicitly configured private Drive folder. It uses the already qualified `/home/tony/.local/bin/rclone` and root-private `/etc/aster-archive-backup/rclone.conf`. It only issues `lsjson` and remote-to-local `copyto`; it does not delete, rename, update or deduplicate Drive objects. It does not access Gmail or request ChatGPT exports.

The user requests/downloads a fresh ChatGPT export and places its ZIP directly in that folder. Its filename must contain `chatgpt` or `export` (case insensitive). The existing archive watcher imports completed deliveries using its configured stable account/source identity. Intake success means a complete ZIP reached that inbox; import and knowledge coverage remain separate database receipts.

## Configuration

Copy `deploy/archive/drive-intake.example.json` to `/etc/aster-archive-backup/drive-intake.json`, owned by root with mode `0600`. Set the exact native hostname, the existing rclone remote name (without a colon), the verified dedicated export-folder ID, and local paths. Keep actual folder/file IDs, hashes and timestamps in that private configuration.

The example uses `/srv/aster-archive/inbox`, `/srv/aster-archive/drive-intake-staging` and `/srv/aster-archive/drive-intake-state`. Create all three before starting the systemd service. Directories must be absolute, root-owned and free of symlink components. Staging and state must have mode `0700`. The inbox must have no group/other write permission. The three directories must be separate, and staging/inbox must share a filesystem for atomic publication.

The script grants UID `10001` read/traverse ACL on the inbox and read ACL on each delivered file using `/usr/bin/setfacl`. Files remain root-owned; original filenames are not used locally. If the watcher is not using the existing bind mount, ensure its UID can also traverse the host path's ancestors.

For a baseline already imported into the canonical archive, add an entry to the private `baseline` array with:

| Field | Meaning |
|---|---|
| `file_id` | Exact Drive ID of the already imported ZIP |
| `sha256` | Known SHA-256 of the complete ZIP |
| `size` | Exact byte length |
| `modified` | Verified Drive modification timestamp, including timezone |

The timestamp is required when Drive does not expose SHA-256. With ID, size and the configured timestamp matching, the script records an `already_imported_baseline` receipt without downloading or delivering that ZIP. When Drive supplies SHA-256, the known hash must match instead. An unproved baseline is blocked rather than silently treated as imported. Equivalent UTC timestamp formats are normalized. A changed version of that same Drive ID remains eligible for intake.

Every rclone child receives `RCLONE_DRIVE_ROOT_FOLDER_ID` independently. Inherited rclone variables are excluded. The private SQLite state is bound to the configured remote, source-folder ID and inbox; changing those requires deliberate separate state. Credentials are never copied into receipts or emitted to the journal.

## Bounds, publication and recovery

The default run lists at most 500 entries, downloads at most two new versions and accepts files up to 8 GiB. Hard configuration ceilings are 2,000 entries, ten downloads and 20 GiB per file. Listings are limited to 2 MiB; subfolders are not traversed. Unsafe names, duplicate paths, duplicate IDs, shortcuts, symlinks and traversal paths fail closed. Nonmatching ordinary files are ignored.

Before each download, the staging filesystem must have at least **10 GiB plus the declared file size** free; small files reserve at least another MiB. The reserve is checked again after download. Each rclone process has a file-size limit, one transfer, an 8 MiB buffer and 16 MiB/s bandwidth limit. This is headroom protection rather than a disk quota against unrelated writers.

A download first lands in private staging. Its size must match, then a fresh complete listing must still show the same path, Drive ID, size, modification time and optional provider SHA-256. The script computes local SHA-256, checks the ZIP envelope and deduplicates by content hash. The canonical importer subsequently performs its own ZIP/content validation; intake does not extract the ZIP.

After file sync and reader ACL installation, a hard link publishes the complete `drive-export-<sha256>.zip` name atomically, without overwriting an existing file. Staging and inbox are on the same filesystem. The inbox directory is synced before the durable SQLite receipt commits. A crash after publication but before the receipt is safe: the next run verifies the existing content before recording it. Failed or interrupted downloads never expose partial inbox files. Under the exclusive intake lock, the next run removes only its own private, exactly named stale partial files.

Receipts retain Drive ID/version, SHA-256 and outcome. Repeated versions are skipped; identical content under a different ID is not delivered again. Files already in the inbox must remain there until their import is complete. The intake script never removes inbox deliveries.

## Service installation

Install the matching service and timer from `deploy/archive/`. Their checkout path is `/home/tony/aster-conversation-archive`; update `ExecStart` if the native checkout differs. If configured directories differ from the example, update the service's exact `ReadWritePaths` accordingly. The credential directory is writable so rclone can refresh its private OAuth token atomically.

Run one foreground qualification before enabling the timer:

```bash
sudo /usr/bin/python3 /home/tony/aster-conversation-archive/scripts/archive-drive-intake.py --config /etc/aster-archive-backup/drive-intake.json
sudo systemctl daemon-reload
sudo systemctl enable --now aster-archive-drive-intake.timer
sudo systemctl list-timers aster-archive-drive-intake.timer
```

The timer checks every 15 minutes with up to 30 seconds of jitter and catches up after downtime. The oneshot has an exclusive filesystem lock, a 512 MiB memory cap, 50% CPU quota, low I/O priority, bounded file size and a 125-minute service deadline. SIGTERM terminates its child process group; systemd's control-group kill covers any remaining child. Failed runs retry at the next timer interval. It does not weaken a failed metadata/hash check or mark that version complete.

Output is limited to counters, status and fixed error codes. No original filenames, Drive IDs, source contents, tokens, credential paths or raw rclone errors are printed. Private state contains the provenance needed to inspect an intake receipt. Preserve that state with the deployment's recovery records; losing it can cause an unnecessary redownload, while content-addressed inbox names still prevent overwrites.

Primary command semantics: [rclone lsjson](https://rclone.org/commands/rclone_lsjson/) and [Google Drive root-folder configuration](https://rclone.org/drive/#root-folder-id).
