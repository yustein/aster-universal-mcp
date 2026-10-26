"""Additive component migrations without changing the canonical v1 checksum."""
from __future__ import annotations

import hashlib
from importlib import resources
import re


_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def migrate_component(store, component: str, version: int, resource_name: str) -> None:
    """Apply one ordered, checksummed packaged migration in its own transaction.

    Call older component versions first when adding another version. A mismatch
    fails closed; neither this registry nor a component edits schema.sql.
    """
    if (not isinstance(component, str) or not _NAME.fullmatch(component)
            or type(version) is not int or not 1 <= version <= 1000
            or not isinstance(resource_name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*\.sql", resource_name)):
        raise ValueError("invalid enrichment migration")
    store.migrate()
    ddl = resources.files("aster_archive").joinpath(resource_name).read_text()
    checksum = hashlib.sha256(ddl.encode()).hexdigest()
    with store.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (717697834003,))
        conn.execute("""CREATE TABLE IF NOT EXISTS aster_archive.enrichment_migrations (
            component text NOT NULL, version integer NOT NULL, checksum text NOT NULL,
            installed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(component,version))""")
        rows = conn.execute("""SELECT version,checksum FROM aster_archive.enrichment_migrations
            WHERE component=%s ORDER BY version""", (component,)).fetchall()
        previous = next((row for row in rows if row["version"] == version), None)
        if previous is not None:
            if previous["checksum"] != checksum:
                raise RuntimeError("enrichment migration checksum mismatch")
            return
        if [row["version"] for row in rows] != list(range(1, version)):
            raise RuntimeError("enrichment migration sequence mismatch")
        conn.execute(ddl)
        conn.execute("""INSERT INTO aster_archive.enrichment_migrations(component,version,checksum)
            VALUES (%s,%s,%s)""", (component, version, checksum))
