#!/usr/bin/env bash
# Install reviewed local assets. Does not start, enable, stop or restart services.
set -euo pipefail
umask 077

if [[ $# != 3 || $(id -u) != 0 ]]; then
  echo 'Usage (as root): install.sh /absolute/coordinator.whl EXPECTED_SHA256 https://coord.example.com' >&2
  exit 2
fi
wheel=$1
expected=$2
origin=$3
asset_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
prefix=/opt/aster-coordinator
state=/var/lib/aster-coordinator
config=/etc/aster-coordinator/service.env
unit=/etc/systemd/system/aster-coordinator.service

[[ $wheel == /* && -f $wheel && ! -L $wheel && $expected =~ ^[a-f0-9]{64}$ ]] || exit 2
actual=$(sha256sum -- "$wheel")
[[ ${actual%% *} == "$expected" ]] || { echo 'Wheel digest mismatch.' >&2; exit 2; }

# Refuse path redirection before any mutation. Preserve an existing config.
python3 - "$asset_dir" "$origin" "$prefix" "$state" "$config" "$unit" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('coordinator_operator',pathlib.Path(sys.argv[1])/'operator.py')
op=importlib.util.module_from_spec(spec); spec.loader.exec_module(op)
op.validate_origin(sys.argv[2])
for value in sys.argv[3:]: op.safe_path(value)
config=pathlib.Path(sys.argv[5])
if config.exists():
    current=op.load_config(config)
    if current['ASTER_MCP_PUBLIC_ORIGIN'] != sys.argv[2]:
        raise SystemExit('Existing configuration origin differs; no changes made.')
PY

python3 "$asset_dir/operator.py" install-check
if getent passwd aster-coordinator >/dev/null; then
  [[ $(id -u aster-coordinator) != 0 ]] || exit 2
else
  useradd --system --user-group --home-dir "$state" --no-create-home --shell /usr/sbin/nologin aster-coordinator
fi
getent group aster-coordinator >/dev/null
install -d -o root -g root -m 0755 "$prefix" "$prefix/releases"
install -d -o root -g aster-coordinator -m 0750 /etc/aster-coordinator
if [[ ! -d $state ]]; then
  install -d -o aster-coordinator -g aster-coordinator -m 0700 "$state"
fi
[[ $(stat -c '%U:%G:%a' "$state") == aster-coordinator:aster-coordinator:700 ]] || {
  echo 'Existing state ownership or mode differs; state was not rewritten.' >&2; exit 2;
}
install -d -o root -g root -m 0700 /var/backups/aster-coordinator /var/lib/aster-coordinator-restore-check

release="$prefix/releases/$expected"
if [[ ! -f $release/complete ]]; then
  [[ ! -e $release ]] || { echo 'Incomplete release directory; inspect before retry.' >&2; exit 2; }
  install -d -o root -g root -m 0755 "$release"
  # Executable code is non-secret and must be traversable by the service user.
  # Keep the surrounding private state/configuration operations under umask 077.
  (
    umask 022
    python3 -m venv "$release/venv"
    "$release/venv/bin/python" -m pip --disable-pip-version-check install -r "$asset_dir/requirements-linux-lock.txt"
    "$release/venv/bin/python" -m pip --disable-pip-version-check install --no-deps "$wheel"
  )
  runuser -u aster-coordinator -- "$release/venv/bin/python" -I -B -c 'import coordination.service'
  printf '%s\n' "$expected" > "$release/complete"
  chmod 0644 "$release/complete"
fi
[[ $(cat "$release/complete") == "$expected" ]] || exit 2
runuser -u aster-coordinator -- "$release/venv/bin/python" -I -B -c 'import coordination.service'
# The managed venv entry is a symlink; all state/configuration paths remain real.
[[ ! -e $prefix/venv || -L $prefix/venv ]] || { echo 'Unmanaged venv directory; preserved.' >&2; exit 2; }
link="$prefix/.venv-new-$$"
trap 'rm -f -- "$link"' EXIT
ln -s "$release/venv" "$link"
mv -Tf "$link" "$prefix/venv"

install -o root -g root -m 0755 "$asset_dir/operator.py" "$prefix/operator.py"
python3 "$prefix/operator.py" prepare-config --config "$config" --origin "$origin"
chown root:aster-coordinator "$config"
chmod 0640 "$config"
runuser -u aster-coordinator -- python3 - "$state" <<'PY'
import os, pathlib, sys
root=pathlib.Path(sys.argv[1])
for name,data in [('approved-clients.json',b'[]\n'),('operator.lock',b'')]:
    path=root/name
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file(): raise SystemExit('Unsafe existing state member.')
        if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o177:
            raise SystemExit('Existing state owner/mode differs; preserved.')
    else:
        with os.fdopen(fd,'wb') as out: out.write(data); out.flush(); os.fsync(out.fileno())
PY
runuser -u aster-coordinator -- python3 "$prefix/operator.py" validate --config "$config"
install -o root -g root -m 0644 "$asset_dir/aster-coordinator.service" "$unit"
systemctl daemon-reload
echo 'Coordinator assets installed. Service remains stopped; approve ingress and start separately.'
