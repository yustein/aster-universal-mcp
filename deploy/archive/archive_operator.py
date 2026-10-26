"""One-shot local owner operations with private SQLite ownership restoration.

Run through operator.sh, which pauses the archive MCP service first. This helper
does not relax or impersonate the existing OAuth helpers' real root check. The
isolated operator container receives only the auth volume and three capabilities
needed to access and restore files owned by the non-root service.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import os
from pathlib import Path
import signal
import stat
import sys


AUTH_DIRECTORY = Path("/var/lib/aster-archive-auth")
SERVICE_UID = SERVICE_GID = 10001
DATABASE = "oauth.sqlite3"
AUTH_FILES = (DATABASE, DATABASE + "-wal", DATABASE + "-shm", DATABASE + "-journal")
LOCK_FILE = ".operator.lock"


class OperatorError(RuntimeError):
    pass


def _open_regular(directory_fd: int, name: str, *, required: bool = False) -> int | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        if required:
            raise OperatorError("Start the archive MCP service once to initialize its OAuth database.") from None
        return None
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid not in (0, SERVICE_UID)):
        os.close(descriptor)
        raise OperatorError("Unexpected OAuth file type or ownership; inspect the private auth volume locally.")
    return descriptor


def _restore(directory_fd: int) -> None:
    # Never recurse or follow paths supplied by a database. SQLite's known
    # sidecars may have been created by root during the operator transaction.
    for name in (*AUTH_FILES, LOCK_FILE):
        descriptor = _open_regular(directory_fd, name, required=name == DATABASE)
        if descriptor is not None:
            try:
                os.fchown(descriptor, SERVICE_UID, SERVICE_GID)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
    os.fchown(directory_fd, SERVICE_UID, SERVICE_GID)
    os.fchmod(directory_fd, 0o700)


def _invoke_helper(command: str, database: Path, *, ttl: int = 300,
                   request_id: str | None = None, approval_code: str | None = None) -> None:
    if command in ("issue", "revoke"):
        import pairing_admin
        helper = pairing_admin.main
        arguments = ["pairing_admin", "--db", str(database), command]
        if command == "issue":
            arguments.extend(["--ttl", str(ttl)])
    elif command in ("inspect", "approve"):
        import owner_approval_cli
        helper = owner_approval_cli.main
        arguments = ["owner_approval_cli", "--db", str(database), command]
        if command == "approve":
            arguments.extend(["--code", approval_code])
        # A valid URL-safe request ID can begin with a hyphen.
        arguments.extend(["--", request_id])
    else:
        raise OperatorError("Unsupported local operator command.")
    previous = sys.argv
    try:
        # Keep the approval code out of OS process arguments and shell history.
        # The existing root-only helper still performs its normal validation.
        sys.argv = arguments
        helper()
    finally:
        sys.argv = previous


def run_operation(command: str, *, auth_directory: Path = AUTH_DIRECTORY,
                  ttl: int = 300, request_id: str | None = None,
                  approval_code: str | None = None) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise OperatorError("This local owner operation requires the isolated root operator context.")
    if command not in ("issue", "revoke", "inspect", "approve"):
        raise OperatorError("Unsupported local operator command.")
    if command == "issue" and (type(ttl) is not int or not 30 <= ttl <= 900):
        raise OperatorError("Pairing lifetime must be 30..900 seconds.")
    if command in ("inspect", "approve") and (
        not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in request_id)
    ):
        raise OperatorError("Invalid pending request identifier.")
    if command == "approve" and (not isinstance(approval_code, str) or not 1 <= len(approval_code) <= 256):
        raise OperatorError("A local one-time pairing code is required.")
    directory_fd = os.open(auth_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    previous_umask = os.umask(0o077)
    try:
        directory = os.fstat(directory_fd)
        if directory.st_uid not in (0, SERVICE_UID):
            raise OperatorError("Unexpected auth-directory owner; inspect the private auth volume locally.")
        lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        lock = os.fstat(lock_fd)
        if not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1 or lock.st_uid not in (0, SERVICE_UID):
            raise OperatorError("Unsafe local operator lock.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperatorError("Another local owner operation is running.") from None
        # Refuse links or unexpected ownership before invoking a root helper.
        for name in AUTH_FILES:
            descriptor = _open_regular(directory_fd, name, required=name == DATABASE)
            if descriptor is not None:
                os.close(descriptor)
        try:
            _invoke_helper(command, Path(auth_directory) / DATABASE, ttl=ttl,
                           request_id=request_id, approval_code=approval_code)
        finally:
            _restore(directory_fd)
    finally:
        os.umask(previous_umask)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local archive OAuth owner operations; use operator.sh")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--ttl", type=int, default=300)
    commands.add_parser("revoke")
    for operation in ("inspect", "approve"):
        commands.add_parser(operation).add_argument("request_id")
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    # Normal container shutdown must unwind the ownership-restoration finally.
    # SIGKILL/host failure cannot run cleanup and require local recovery.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, terminate)
    code = None
    if args.command == "approve":
        if not sys.stdin.isatty():
            parser.error("approval requires an interactive local terminal")
        code = getpass.getpass("One-time local pairing code: ")
    try:
        run_operation(args.command, ttl=getattr(args, "ttl", 300),
                      request_id=getattr(args, "request_id", None), approval_code=code)
    except (OperatorError, OSError):
        # Detailed file paths or SQLite state do not belong in logs. The local
        # operator can inspect the protected volume if this bounded flow fails.
        raise SystemExit("Local archive owner operation failed; inspect the private auth volume and ownership.") from None


if __name__ == "__main__":
    main()
