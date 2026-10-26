# Native archive recovery qualification

These procedures are scoped to the native Lenomila archive. They do not touch
the PA host, start retired VMs, publish an MCP endpoint, or enroll clients.
Run host commands as the authorized operator. A script being present is not a
passing recovery result: retain its metadata-only completion receipt.

## What is proved

The recovery chain has three distinct checks:

1. Restore the completed real-corpus backup into a new PostgreSQL database and
   three separate Docker volumes, then query it using the actual restricted
   reader role. Compare coverage, exact passage hashes, source offsets, branch
   context, the original export SHA256, OAuth SQLite integrity, and client ACL.
2. Encrypt that exact backup with restic, upload it to the verified private
   offsite repository, download the same snapshot into a new private
   directory using a separately retrieved escrow-key copy, and compare all
   backup checksums. This links the independent copy to the bytes whose actual
   database and source restore already passed.
3. Capture a host reboot checkpoint and, after an operator-issued reboot, prove
   the host boot ID changed, the same persistent containers and volumes recovered
   without manual starts, required control services are active, VMs remain off,
   and the reader produces the same evidence.

This is logical restore, offsite round-trip, and clean host reboot qualification.
It does not claim a bare-metal reinstall or an injected power-loss test.

## 1. Qualify the actual local backup

Use the exact completed backup and its exact completed import ID. Replace the
placeholders with paths and identities from the private completion receipts.

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-restore-qualify.py \
  --backup /home/tony/aster-archive-backups/EXACT_COMPLETED_BACKUP \
  --import-id EXACT_COMPLETED_IMPORT_ID
```

The helper uses existing PostgreSQL and archive images; no rebuild is needed.
It checks the three manifest entries before creating test resources, and
requires a conservative free-space estimate plus a 10 GiB reserve. New database
and volume names contain a random run identity. The database gets a matching
comment; volumes get a matching Docker label.

For dumps containing pgvector, the database image must have the recorded vector
extension available. The helper creates that extension as the database
administrator, then restores application objects as the ordinary ingest role.
Only the reviewed extension CREATE and COMMENT entries are removed from the
restore list; ordinary object comments and data are retained. No extension
ownership or elevated database privilege is granted to the ingest role. An
unknown extension fails qualification before test resources are created.

The live archive stays running. Initial baseline capture should precede unrelated
client-grant changes, because the selected backup's ACL is compared with that
baseline. Once a restore passes, preserve that evidence; a later grant change
does not invalidate the historical restore result.

Success cleans only the marked test database and volumes and retains private
`baseline.json`, `restore-proof.json`, `state.json`, and operations logs under
`deploy/archive/private/recovery/`. Failures preserve the checkpoint and test
targets. Inspect the saved stage and private log; do not print the log wholesale.

Explicit cleanup of a failed or deliberately retained test is:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-restore-qualify.py \
  --cleanup /home/tony/aster-conversation-archive/deploy/archive/private/recovery/EXACT_RUN/state.json
```

Cleanup refuses reused names with mismatched markers. It does not force database
connections closed, delete unlabelled targets, prune Docker globally, or remove
the qualified backup.

## 2. Configure independent encrypted storage and key escrow

The default Drive backend needs the qualified rclone binary, an explicitly identified working
rclone configuration, and restic with repository-version-2 support. Check the
installed versions and package provenance on the host before starting. The
wrapper uses `/home/tony/.local/bin/rclone` rather than silently selecting another
binary from `PATH`.

Copy the example `deploy/archive/offsite.example.json` to the new private
configuration directory `/etc/aster-archive-backup/`, using mode `0600` and root
ownership. Do not overwrite an existing configuration or key. Fill its fields
with verified values:

| Field | Required value |
| --- | --- |
| `repository_type` | `rclone` for Drive; omitted in legacy configurations means `rclone` |
| `repository` | `rclone:ty gdrive:aster-archive-restic` |
| `drive_root_folder_id` | Exact verified private Drive parent folder ID |
| `password_file` | Root-private file containing the generated high-entropy restic password |
| `rclone_program` | Qualified `/home/tony/.local/bin/rclone` executable |
| `rclone_config` | Root-private copy of the qualified configuration, normally `/etc/aster-archive-backup/rclone.conf` |
| `restic_program` | Exact installed executable path |
| `backup_dir` | Root-private completed local backup directory |
| `state_dir` | Root-private receipts/cache directory, normally `/var/lib/aster-archive-backup` |
| `key_escrow.reference` | Verified private ChatGPT recovery-key file reference |
| `key_escrow.password_sha256` | Digest of the exact password-file bytes |
| `key_escrow.verified` | Set true only after the separate private escrow upload is verified |

The password itself must not appear in JSON configuration, command arguments,
Git, logs, image layers, or chat text. The operator retains a local `0600` copy
for unattended operation and a separately recoverable copy in Tony's private
ChatGPT file account. The ciphertext repository is independent of that escrow. Preserve
the repository locator and recovery instructions with the escrow record.

Copy the qualified rclone configuration privately rather than running scheduled
root processes against Tony's active user configuration. Token refresh may
rewrite the selected file. A separate root-owned copy preserves the working
user configuration and its ownership. The binary and checkout remain part of
the trusted operator deployment; Tony already administers this host with sudo.

On a replacement host, Google Drive must be reauthorized separately; possession
of the restic password does not grant Google-account access. A complete recovery
also needs the archived `deployment.env`, PostgreSQL/image versions, and archive
source code. The encrypted backup contains deployment configuration, originals,
database dump, and authentication state. It does not contain the restic password.

The wrapper exports explicit `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`,
`RCLONE_CONFIG`, and a private cache path to child processes. It clears competing
password and repository-file settings. Restic starts rclone over stdio; no extra
HTTP server or public listener is needed.
`RCLONE_DRIVE_ROOT_FOLDER_ID` is set only in those child processes, so the dedicated
repository resolves under the verified private backup folder without changing
the user's original remote configuration. The repository pin also records this
folder ID. Preserve it with the recovery metadata.
For this dedicated restic child only, `RCLONE_DRIVE_USE_TRASH=false` makes approved
pruning permanently remove unreferenced ciphertext instead of leaving it in
Drive trash. It does not alter the user's global rclone configuration.

Initialize only the dedicated new repository:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-offsite.py \
  --config /etc/aster-archive-backup/offsite.json init
```

If the operator already initialized that exact repository, use `register` instead
of `init`. Registration reads and pins its repository ID. Subsequent operations
refuse an unexpected repository identity.

### Optional dedicated SFTP repository

The SFTP backend is an explicit alternative when the operator has qualified a
separate destination and its capacity. It does not initialize a server or change
SSH configuration. Use `deploy/archive/offsite-sftp.example.json`, retain the
same private restic password/independent escrow checks, and provide these fields:

| Field | Exact value |
| --- | --- |
| `repository_type` | `sftp` |
| `repository` | `sftp:aster-archive-backup:/repository` |
| `ssh_config` | `/etc/aster-archive-backup/ssh_config` |
| `ssh_known_hosts` | `/etc/aster-archive-backup/known_hosts` |
| `ssh_identity_file` | `/etc/aster-archive-backup/id_ed25519` |

The remote account is confined to the root-owned chroot
`/srv/aster-archive-offsite`; `/repository` is its client-visible writable
directory, physically `/srv/aster-archive-offsite/repository`. Qualify the
dedicated account with forced `internal-sftp`, public-key authentication only,
and no shell, TTY, or forwarding. Verify the server's Ed25519 host key through an
independent established administration route before creating `known_hosts`.
The wrapper does not treat an unverified network key scan as a trust decision.

All three local SSH files and their common directory must be root-private
regular files/directory. The identity must be a dedicated unattended Ed25519 key.
Use this single-host SSH configuration, replacing only the verified hostname,
dedicated account name, and port if needed:

```sshconfig
Host aster-archive-backup
    HostName REPLACE_WITH_VERIFIED_HOSTNAME
    User REPLACE_WITH_DEDICATED_ACCOUNT
    Port 22
    IdentityFile /etc/aster-archive-backup/id_ed25519
    UserKnownHostsFile /etc/aster-archive-backup/known_hosts
    GlobalKnownHostsFile /dev/null
    HostKeyAlias aster-archive-backup
    HostKeyAlgorithms ssh-ed25519
    BatchMode yes
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    IdentityAgent none
    PreferredAuthentications publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    UpdateHostKeys no
    VerifyHostKeyDNS no
    ForwardAgent no
    ClearAllForwardings yes
    RequestTTY no
    ControlMaster no
    ControlPath none
    ControlPersist no
    PermitLocalCommand no
    ConnectTimeout 15
    ConnectionAttempts 1
    ServerAliveInterval 30
    ServerAliveCountMax 3
    LogLevel ERROR
```

`known_hosts` must contain exactly one verified key line, with the alias as its
first field: `aster-archive-backup ssh-ed25519 VERIFIED_PUBLIC_KEY_BASE64`.
The wrapper rejects Includes, extra hosts, executable configuration directives,
password fallback, and unpinned host-key settings. It uses system OpenSSH and
the official restic option `sftp.args=-F /etc/aster-archive-backup/ssh_config`,
limits SFTP connections to two, clears inherited SSH agent/askpass settings,
and never needs rclone configuration or a Drive folder ID in this mode.

Before every SFTP upload, batch `df /repository` obtains the server's
`statvfs@openssh.com` capacity report without a remote shell command. A missing
extension, failed request, unrecognized report, or less than **50 GiB plus the
exact four-file backup size** of unprivileged available space fails closed
before `restic backup`. The private receipt records only capacity numbers.
This is a measured headroom guard, not a quota or space reservation against
concurrent server workloads; qualify server-side isolation separately.

The repository pin records backend type, repository ID/locator, and hashes of
the exact SSH configuration, known-hosts file, and identity. Changing these
requires explicit operator review and a new qualified state; registration does
not silently overwrite an old pin. Use a fresh state directory when switching
from an already registered Drive repository. The SFTP example uses
`/var/lib/aster-archive-backup-sftp`; pass that same directory with
`--offsite-state-dir` to the reboot checker. Timers must select the intended
private configuration file. Retention, independent escrow-key recovery,
exact-file checks, and snapshot round-trip proof stay the same for both backends.

## 3. Upload and prove the independent copy

Upload the exact qualified local backup and link its successful restore state:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-offsite.py \
  --config /etc/aster-archive-backup/offsite.json upload \
  --backup /home/tony/aster-archive-backups/EXACT_COMPLETED_BACKUP \
  --restore-state /home/tony/aster-conversation-archive/deploy/archive/private/recovery/EXACT_RUN/state.json
```

Record the full snapshot ID from its final JSON. Upload success must include a
successful repository structure check. A nonzero restic exit, including an
incomplete backup, does not produce a successful receipt.
Only the four managed backup files are accepted. The linked actual-restore
checkpoint must contain `backup_manifest_sha256`, recorded when that exact local
backup passed checksum validation; upload compares it with the current manifest.
If qualifying with an older helper, the operator must establish that binding
from the unchanged tested backup before attaching its proof.

Retrieve the escrowed password through the private file account to a separate
root-private temporary file. This must be a separate retrieval, not another copy
of the local operational password. Keep the bytes unchanged. Then run:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-offsite.py \
  --config /etc/aster-archive-backup/offsite.json \
  --recovery-password-file /run/aster-archive-recovered-password \
  roundtrip --snapshot EXACT_FULL_SNAPSHOT_ID
```

This downloads the entire selected backup to a new private directory and
verifies `database.dump`, `files.tar.gz`, `deployment.env`, and the exact checksum
manifest. The original live files are not used as the download source. On
success, the disposable download is removed; metadata receipts remain. Remove
the temporary escrow password file after the test, preserving its independent
escrow and the operational copy.

The first successful round-trip linked to an actual restore becomes the
protected recovery anchor. The wrapper does not repeat the already-passed
database restore merely because the same bytes traveled through encrypted cloud
storage. A second physical database instantiation, if desired, should use the
saved original baseline rather than later unrelated live ACL changes.

Default `restic check` verifies repository structure. To read and verify every
repository pack as well, run `integrity --full`; the monthly timer below does
this automatically. The explicit snapshot download plus original checksums
proves all files in that selected snapshot, even when other snapshots exist.

### Recovering the backup bytes after losing host-local state

The normal wrapper intentionally requires its local configuration, pinned
repository ID, and snapshot receipts. It is a qualification and scheduling
tool, not a substitute for cold recovery instructions. Keep the following
nonsecret metadata beside the independent password escrow: exact repository
locator, qualified snapshot ID, repository ID, source commit, PostgreSQL/archive
image versions, and the owner/source/import identities. Retain the completed
restore proof and its manifest digest there as well.

For a Drive repository on a replacement host, install trusted restic/rclone binaries and authorize
Google Drive through the user's account. Recover the escrow password to a
private file. These direct commands require neither the old operational key
file nor `/var/lib/aster-archive-backup`:

```bash
umask 077
export RESTIC_REPOSITORY='rclone:ty gdrive:aster-archive-restic'
export RCLONE_DRIVE_ROOT_FOLDER_ID=EXACT_VERIFIED_PRIVATE_FOLDER_ID
export RESTIC_PASSWORD_FILE=/secure/recovered-restic-password
export RCLONE_CONFIG=/secure/newly-authorized-rclone.conf
restic -o rclone.program=/verified/path/to/rclone snapshots \
  --host lenomila-aster-archive --tag aster-archive
```

Use an exact snapshot ID from the escrow record or decrypted listing and a new
empty private target directory:

```bash
restic -o rclone.program=/verified/path/to/rclone restore EXACT_FULL_SNAPSHOT_ID \
  --target /secure/new-empty-restore-directory
```

For the SFTP alternative, independently recover or reauthorize the dedicated SSH
identity and verified host-key record, recreate the private SSH configuration,
and use `RESTIC_REPOSITORY='sftp:aster-archive-backup:/repository'` with
`restic -o 'sftp.args=-F /etc/aster-archive-backup/ssh_config'` for the same
`snapshots` and `restore` commands. The restic password alone does not grant SSH
access. Preserve the physical/chroot path mapping and separate SSH recovery
instructions with the escrow record; the four-file data backup does not contain
the offsite SSH key or its server account configuration.

The absolute backup path is recreated below that target. Validate its
`SHA256SUMS` and compare the manifest digest with the independently retained
restore proof. Recover `deployment.env` privately; it contains database secrets.
Rebuild an isolated archive deployment from the recorded source/image versions,
restore the dump and separate volumes using the tested procedure, and verify
source hashes and scoped retrieval before promoting it. The current native test
has not executed this whole replacement-host installation.

## 4. Schedule and retain backups

After the recovery anchor exists, install the three service/timer pairs from
`deploy/archive/` into `/etc/systemd/system/`. Inspect their fixed host paths,
then run native `systemd-analyze verify` before enabling them.

```bash
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now aster-archive-backup.timer
sudo -n systemctl enable --now aster-archive-backup-retention.timer
sudo -n systemctl enable --now aster-archive-backup-integrity.timer
sudo -n systemctl list-timers 'aster-archive-backup*'
```

Daily backup runs at 03:15 UTC with up to 15 minutes of delay. Weekly retention
runs Sunday at 09:00 UTC; full repository verification runs on day 1 at 12:00 UTC.
Timers catch up missed runs. Host orchestration is limited to half a CPU and
1 GiB; the archive containers keep their separately configured limits.

All wrapper operations share a nonblocking lock. A failed upload preserves its
exact completed local backup for retry. The daily service retries after 30
minutes, with at most three attempts per six hours. Writer stop/resume behavior
belongs to the existing consistent-backup helper, which resumes only archive
writers that were running beforehand: `watch`, `worker`, `embedding`,
`knowledge`, and `cognee`. This freezes the mutable Cognee graph/receipt directory
with the canonical database and original files. Public reproducible model files
live in the separate `/var/lib/aster-archive-models` volume and are rebuilt from
their pinned manifests; they are not part of the archive backup. Restore
extraction continues to reject symbolic and hard links. Coordinate direct manual calls to the
older local backup helper with scheduled runs; it predates the offsite lock.
If upload succeeds but local housekeeping fails, retry resumes the saved upload
receipt without creating another dump or uploading another snapshot. A requested
remote prune is retryable even when a prior attempt already forgot the selected
snapshot records.

Before creating another local backup, the daily wrapper checks estimated
database/original size and the 10 GiB free-space reserve. It never deletes
unuploaded local backups to make a failed job appear successful. Once the new
upload succeeds, it retains three newest managed local backups plus the local
recovery anchor. Cleanup is limited to root-private, recorded, unchanged backup
directories containing exactly the four expected files. Unrecorded backups and
operator-augmented directories are preserved.

Remote retention starts with a dry-run policy: 7 daily, 4 weekly, 6 monthly,
and at least 2 newest snapshots, grouped by this archive host and tag. The wrapper
also removes the qualified anchor from every deletion proposal. Before applying
an exact list of snapshot IDs it checks the repository; after optional pruning,
it checks again. With only the initial snapshot, it deletes nothing.

Preview retention without deleting anything:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-offsite.py \
  --config /etc/aster-archive-backup/offsite.json retention
```

The weekly service applies the reviewed policy. Errors remain visible in the
failed systemd service and private JSON checkpoint. Human notification delivery
is not configured by these scripts.

## 5. Qualify a real host reboot

Finish active imports, backup/recovery work, and other host jobs first. Read the
actual RDC, native control, and tunnel unit names from the host; do not invent
them. The preflight only reads service/VM/container/database state and writes its
private checkpoint. It never issues a reboot or changes access.

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-reboot-check.py pre \
  --checkpoint /var/lib/aster-archive-backup/host-reboot-EXACT_RUN.json \
  --import-id EXACT_COMPLETED_IMPORT_ID \
  --unit EXACT_RDC_UNIT.service \
  --unit EXACT_NATIVE_CONTROL_UNIT.service \
  --unit EXACT_TUNNEL_UNIT.service
```

`docker.service` is checked automatically. The checkpoint requires the actual
restore and independent escrow-key round-trip to have passed. It verifies
archive restart policies, persistent volume identities, source retrieval,
stopped VMs, and no VM autostart. A VM autostart failure is for the operator to
resolve explicitly; the helper does not change VM definitions. Review any other
running containers listed in the result and coordinate their work before reboot.

Legacy deployments without enrichment containers retain the canonical reader
check. Any existing enrichment container must be running with the persistent
restart policy. A running `embedding` service also requires a real local E5
semantic search, exact passage fetch/hash validation, and cross-owner denial.
A running `knowledge` service requires a searchable derived record and an exact
source citation verified against its canonical passage. A running `cognee`
service requires a persisted completed pilot receipt; this is a persisted-state
check, not a new graph inference run. It does not turn inferred graph records
into verified decisions.

To require the entire enabled stack explicitly, append these options to the
preflight command:

```text
--require-enrichment embedding --require-enrichment knowledge --require-enrichment cognee
```

Wait for at least one successful item in each required component before this
qualification. Full-corpus indexing can continue afterward. The postflight
requires the same generation/pipeline identities, nondecreasing completed
derived counts, no increase in failed items, and successful bounded queries.
It permits background indexing progress while preserving exact canonical
counts, hashes, offsets, and scope checks. The checkpoint retains both proofs;
outputs contain metadata and booleans, never conversation text or titles.

A passing preflight opens a 15-minute reboot window and prevents scheduled
offsite work from starting during that window and a short recovery grace period.
The postflight binds its marker and token to that exact checkpoint and verifies
the new host boot timestamp falls within the window, allowing three minutes for
shutdown. The operator uses the already
authorized host reboot only while those observations remain current. Maintain
the working RDC route for reconnecting; do not change access or firewall rules
as part of this check.

After reconnecting, run the postflight **before manually starting any archive
container**:

```bash
sudo -n python3 /home/tony/aster-conversation-archive/scripts/archive-reboot-check.py post \
  --checkpoint /var/lib/aster-archive-backup/host-reboot-EXACT_RUN.json
```

A container restart alone cannot pass: the Linux host boot ID must differ. The
same container IDs, images, volumes, counts, exact passage evidence, and scoped
denial must survive. Required control units and other previously running
containers must recover. The machine-readable receipt reports those observable
facts and verified restart policies. The operator's controlled procedure, with
no manual starts before verification, establishes automatic recovery; container
status alone cannot distinguish an automatic start from an operator's start.
The helper performs no repairs; a failure remains an
honest failed checkpoint. Finally, the operator should issue one harmless
identity command through each restored remote-control avenue to prove actual
transport execution, since unit status alone does not prove end-to-end access.

## Dedicated archive tunnel recovery record

The four-file backup manifest remains the archive data/authentication format.
Recovery of the separately configured archive tunnel needs its own versioned,
private encrypted operator bundle. Record only that tunnel's credentials and
configuration, exact systemd units and ownership/modes, public origin, loopback
port and trusted proxy peer, and the source/image versions needed to rebuild it.
Keep this distinct from PA and unrelated control-service credentials. Preserve
its manifest and recovery location with the independently escrowed recovery
instructions.

This document specifies the additional recovery record; the archive backup
scripts do not yet create or restore it. On a replacement host, verify the local
reader and OAuth state before restoring public routing. Do not treat successful
archive-data recovery as proof that external DNS, tunnel access, or remote
control has been reconstructed.

## Primary references

- [Restic repository preparation and rclone backend](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [Restic repository integrity checks](https://restic.readthedocs.io/en/stable/045_working_with_repos.html)
- [Restic snapshot restoration](https://restic.readthedocs.io/en/stable/050_restore.html)
- [Restic retention and pruning](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Restic scripting and JSON interfaces](https://restic.readthedocs.io/en/stable/075_scripting.html)
- [Rclone Google Drive deletion settings](https://rclone.org/drive/#deleting-files)
- [Restic SFTP configuration](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#sftp)
- [OpenSSH client configuration](https://man.openbsd.org/ssh_config.5)
- [OpenSSH SFTP batch and statvfs capacity commands](https://man.openbsd.org/sftp.1)
