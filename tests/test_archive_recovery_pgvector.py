"""Real pg_dump/pg_restore privilege regression; synthetic isolated databases only."""
import importlib.util
import os
from pathlib import Path
import subprocess
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("restore_pgvector_test", ROOT / "scripts/archive-restore-qualify.py")
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def test_pgvector_restore_preserves_admin_extension_and_ordinary_comments(tmp_path):
    dsn = os.environ.get("ASTER_ARCHIVE_RECOVERY_TEST_DSN")
    bindir = Path(os.environ.get("ASTER_ARCHIVE_RECOVERY_PG_BIN", "/usr/bin"))
    if not dsn or not (bindir / "pg_dump").exists() or not (bindir / "pg_restore").exists():
        pytest.skip("requires disposable local PostgreSQL with pgvector and its client binaries")
    tag = uuid.uuid4().hex[:16]
    names = ["aster_recovery_source_" + tag, "aster_recovery_target_" + tag]
    role = "aster_recovery_writer_" + tag
    values = conninfo_to_dict(dsn)
    created = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        assert admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
        assert admin.execute("SELECT count(*) FROM pg_available_extensions WHERE name='vector'").fetchone()[0] == 1
        admin_name = admin.execute("SELECT current_user").fetchone()[0]
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(role)))
        try:
            for name in names:
                admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(sql.Identifier(name), sql.Identifier(role)))
                created.append(name)
            source_dsn = make_conninfo(**(values | {"dbname": names[0]}))
            target_dsn = make_conninfo(**(values | {"dbname": names[1]}))
            with psycopg.connect(source_dsn, autocommit=True) as source:
                source.execute(restore.EXTENSION_DDL["vector"].decode())
                source.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                source.execute("CREATE TABLE public.synthetic_recovery(id integer PRIMARY KEY, embedding public.vector(3))")
                source.execute("COMMENT ON TABLE public.synthetic_recovery IS 'synthetic table annotation must survive'")
                source.execute("INSERT INTO public.synthetic_recovery VALUES (1,'[1,2,3]')")
            archive = tmp_path / "synthetic.dump"
            subprocess.run([str(bindir / "pg_dump"), "--dbname", source_dsn, "--format=custom", "--no-owner", "--no-acl",
                            "--file", str(archive)], stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30)
            listing = subprocess.run([str(bindir / "pg_restore"), "--list", str(archive)], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, check=True, timeout=30).stdout
            filtered, extensions = restore.prepare_restore_list(listing)
            assert extensions == ["vector"]
            assert "COMMENT - EXTENSION vector" in listing and "COMMENT - EXTENSION vector" not in filtered
            toc = tmp_path / "restore.list"
            toc.write_text(filtered)
            with psycopg.connect(target_dsn, autocommit=True) as target:
                for extension in extensions:
                    target.execute(restore.EXTENSION_DDL[extension].decode())
            subprocess.run([str(bindir / "pg_restore"), "--dbname", target_dsn, "--exit-on-error", "--no-owner", "--no-acl",
                            "--role", role, "--use-list", str(toc), str(archive)], stdin=subprocess.DEVNULL,
                           capture_output=True, check=True, timeout=30)
            with psycopg.connect(target_dsn) as target:
                assert target.execute("SELECT embedding::text FROM public.synthetic_recovery WHERE id=1").fetchone()[0] == "[1,2,3]"
                assert target.execute("SELECT obj_description('public.synthetic_recovery'::regclass)").fetchone()[0] == "synthetic table annotation must survive"
                assert target.execute("SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname='vector'").fetchone()[0] == admin_name
                assert target.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s", (role,)).fetchone() == (False, False, False)
        finally:
            for name in reversed(created):
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
