#!/usr/bin/env python3
"""
xtrabackup option tests: renamed options, deprecated options and --history
written to another server.

Ported from the PXB dev suite (storage/innobase/xtrabackup/test):
  - PXB-3758  t/apply_redo_only_alias.sh       -> test_apply_redo_only_alias
  - PXB-3840  t/incremental_option_aliases.sh  -> test_incremental_option_aliases
  - PXB-3788  t/deprecated_options.sh          -> test_deprecated_options_warn
  - PXB-2559  t/history_dsn.sh                 -> test_history_on_other_server

Assumption: PS/MS and PXB are already installed (tarball or build tree).
"""

import os
import re
import shutil
from typing import Optional

import pytest
from test_helper import CORE_FILE_OPT, MYSQL_PORT, BackupTestHelper

# Release lines that shipped these fixes, and the first fixed version on each.
FIXED_IN_8_4_0_8 = {"8.4": "8.4.0-8", "9.7": "9.7.2-2"}

# Ports for the extra servers some tests start next to the primary.
HISTORY_PORT = MYSQL_PORT + 50
RESTORE_PORT = MYSQL_PORT + 51


@pytest.fixture(scope="function")
def test_helper(request):
    """Create a test helper instance for each test."""
    test_name = request.node.name if hasattr(request, "node") else None
    helper = BackupTestHelper(test_name=test_name)
    helper.server_version, helper.server_version_normalized = helper.get_mysql_version()
    helper.xtrabackup_version, helper.xtrabackup_version_normalized = helper.get_xtrabackup_version()
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir(test_helper):
    """Ensure log directory exists."""
    if not os.path.exists(test_helper.logdir):
        os.makedirs(test_helper.logdir)


# ============================================================================
# Helpers
# ============================================================================

def _xtrabackup(test_helper, args: list, log_name: str) -> tuple:
    """Run xtrabackup with ``args``; return (returncode, combined output, log file)."""
    log_file = os.path.join(test_helper.logdir, f"{log_name}_log")
    cmd = test_helper._xtrabackup_cmd_prefix() + ["--no-defaults", *args]
    result = test_helper.run_command(cmd, check=False, log_file=log_file)
    return result.returncode, (result.stdout or "") + (result.stderr or ""), log_file


def _backup_args(test_helper, target_dir: str) -> list:
    # xtrabackup creates the target directory itself, but not its parents.
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    return [
        f"--user={test_helper.backup_user}", "--password=", "--backup",
        f"--target-dir={target_dir}", f"-S{test_helper.socket_path}",
        f"--datadir={test_helper.datadir}", *CORE_FILE_OPT.split(),
    ]


def _backup(test_helper, target_dir: str, log_name: str, extra: Optional[list] = None) -> str:
    """Take a backup that must succeed; return its output."""
    rc, out, log_file = _xtrabackup(test_helper, _backup_args(test_helper, target_dir) + (extra or []), log_name)
    if rc != 0:
        pytest.fail(f"ERR: backup '{log_name}' failed. Log: {log_file}")
    return out


def _prepare(test_helper, target_dir: str, log_name: str, extra: Optional[list] = None) -> str:
    """Prepare a backup that must succeed; return its output."""
    args = ["--prepare", f"--target-dir={target_dir}", *CORE_FILE_OPT.split()] + (extra or [])
    rc, out, log_file = _xtrabackup(test_helper, args, log_name)
    if rc != 0:
        pytest.fail(f"ERR: prepare '{log_name}' failed. Log: {log_file}")
    return out


def _checkpoint(target_dir: str, key: str) -> str:
    """Read ``key`` from a backup's xtrabackup_checkpoints file."""
    with open(os.path.join(target_dir, "xtrabackup_checkpoints"), encoding="utf-8") as f:
        for line in f:
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip()
    return ""


def _create_table(test_helper, first: int, last: int) -> None:
    """Create test.t1 if needed and insert rows first..last."""
    test_helper.primary.mysql("CREATE TABLE IF NOT EXISTS test.t1 (a INT PRIMARY KEY) ENGINE=InnoDB")
    values = ",".join(f"({i})" for i in range(first, last + 1))
    test_helper.primary.mysql(f"INSERT INTO test.t1 (a) VALUES {values}")


def _value(server, query: str) -> str:
    """Run a single-value query on ``server``; return the value without its column header."""
    out = server.mysql(query, capture=True) or ""
    return out.splitlines()[-1] if out else ""


def _deprecation(old: str, new: str) -> str:
    return f"--{old} is deprecated and will be removed in a future release. Please use --{new} instead."


def _same_option(old: str, new: str) -> str:
    return f"--{old} and --{new} are the same option; pass only one."


# ============================================================================
# PXB-3758: --apply-redo-only is the new name of --apply-log-only
# ============================================================================

def test_apply_redo_only_alias(test_helper):
    """PXB-3758: --apply-redo-only stops after redo apply like --apply-log-only
    did; the old name still works but warns once; passing both is rejected;
    --help lists the new name and marks the old one deprecated.
    """
    test_helper.require_pxb_fix("PXB-3758", FIXED_IN_8_4_0_8)
    test_helper.initialize_db()
    _create_table(test_helper, 1, 3)

    full = os.path.join(test_helper.backup_dir, "full")
    _backup(test_helper, full, "full_backup")
    full_old = f"{full}_old_name"
    full_both = f"{full}_both_names"
    shutil.copytree(full, full_old)
    shutil.copytree(full, full_both)

    print("Test: --apply-redo-only stops after redo apply, without any warning")
    out = _prepare(test_helper, full, "prepare_redo_only", ["--apply-redo-only"])
    assert "apply-log-only is deprecated" not in out, "--apply-redo-only emitted the --apply-log-only deprecation warning"
    assert _checkpoint(full, "backup_type") == "log-applied", "--apply-redo-only did not stop after redo apply"

    print("Test: --apply-log-only still works and warns exactly once")
    out = _prepare(test_helper, full_old, "prepare_log_only", ["--apply-log-only"])
    warning = _deprecation("apply-log-only", "apply-redo-only")
    assert out.count(warning) == 1, f"expected one '{warning}' warning, got {out.count(warning)}"
    assert _checkpoint(full_old, "backup_type") == "log-applied", "--apply-log-only did not stop after redo apply"

    print("Test: passing both spellings is rejected before anything is applied")
    rc, out, log_file = _xtrabackup(
        test_helper,
        ["--prepare", f"--target-dir={full_both}", "--apply-redo-only", "--apply-log-only"],
        "prepare_both_names",
    )
    assert rc != 0, f"--apply-redo-only together with --apply-log-only was accepted. Log: {log_file}"
    assert _same_option("apply-log-only", "apply-redo-only") in out, f"missing 'same option' error. Log: {log_file}"
    assert _checkpoint(full_both, "backup_type") == "full-backuped", "the rejected prepare still modified the backup"

    print("Test: --help shows --apply-redo-only and marks --apply-log-only deprecated")
    _, out, _ = _xtrabackup(test_helper, ["--help"], "help")
    assert re.search(r"^\s+--apply-redo-only\s", out, re.MULTILINE), "--help does not list --apply-redo-only"
    assert re.search(r"--apply-log-only\s+\(deprecated\)", out), "--help does not mark --apply-log-only deprecated"


# ============================================================================
# PXB-3840: phase-explicit names for the incremental options
# ============================================================================

def test_incremental_option_aliases(test_helper):
    """PXB-3840: --backup-incremental-base / --prepare-incremental-from-dir
    replace --incremental-basedir / --incremental-dir. A full + 2 incremental
    chain mixing new and old names restores correctly; only the old names
    warn, once each; an old name together with its new one is rejected on
    both --backup and --prepare.
    """
    test_helper.require_pxb_fix("PXB-3840", FIXED_IN_8_4_0_8)
    test_helper.initialize_db()
    _create_table(test_helper, 1, 3)

    full = os.path.join(test_helper.backup_dir, "full")
    inc1 = os.path.join(test_helper.backup_dir, "inc1")
    inc2 = os.path.join(test_helper.backup_dir, "inc2")
    old_backup = ("incremental-basedir", "backup-incremental-base")
    old_prepare = ("incremental-dir", "prepare-incremental-from-dir")

    _backup(test_helper, full, "full_backup")
    _create_table(test_helper, 4, 6)
    print("Test: incremental backup with --backup-incremental-base, no warning")
    out = _backup(test_helper, inc1, "inc1_backup", [f"--backup-incremental-base={full}"])
    assert "is deprecated" not in out, "the new option name emitted a deprecation warning"

    _create_table(test_helper, 7, 9)
    print("Test: incremental backup with --incremental-basedir still works and warns once")
    out = _backup(test_helper, inc2, "inc2_backup", [f"--incremental-basedir={inc1}"])
    assert out.count(_deprecation(*old_backup)) == 1, "expected one --incremental-basedir deprecation warning"

    print("Test: --incremental-basedir together with --backup-incremental-base is rejected")
    rejected = os.path.join(test_helper.backup_dir, "inc_both_names")
    rc, out, log_file = _xtrabackup(
        test_helper,
        _backup_args(test_helper, rejected) + [f"--incremental-basedir={inc1}", f"--backup-incremental-base={inc1}"],
        "inc_both_names_backup",
    )
    assert rc != 0, f"both incremental base options were accepted on --backup. Log: {log_file}"
    assert _same_option(*old_backup) in out, f"missing 'same option' error. Log: {log_file}"
    assert not os.path.exists(os.path.join(rejected, "xtrabackup_checkpoints")), "the rejected backup still ran"

    print("Test: prepare the chain with the new name, the old name, and both")
    _prepare(test_helper, full, "prepare_full", ["--apply-redo-only"])
    out = _prepare(test_helper, full, "prepare_inc1", ["--apply-redo-only", f"--prepare-incremental-from-dir={inc1}"])
    assert "is deprecated" not in out, "the new option name emitted a deprecation warning"
    rc, out, log_file = _xtrabackup(
        test_helper,
        ["--prepare", f"--target-dir={full}", "--apply-redo-only", f"--incremental-dir={inc2}",
         f"--prepare-incremental-from-dir={inc2}"],
        "prepare_both_names",
    )
    assert rc != 0, f"both incremental dir options were accepted on --prepare. Log: {log_file}"
    assert _same_option(*old_prepare) in out, f"missing 'same option' error. Log: {log_file}"
    out = _prepare(test_helper, full, "prepare_inc2", ["--apply-redo-only", f"--incremental-dir={inc2}"])
    assert out.count(_deprecation(*old_prepare)) == 1, "expected one --incremental-dir deprecation warning"
    _prepare(test_helper, full, "prepare_final")

    print("Test: the restored chain has every row")
    expected = _value(test_helper.primary, "CHECKSUM TABLE test.t1")
    restored = test_helper.create_replica(name="restored", server_id=2, port=RESTORE_PORT)
    test_helper.restore_backup_to(restored.datadir, "", "aliases")
    restored.start()
    assert _value(restored, "SELECT COUNT(*) FROM test.t1") == "9", "restored chain is missing rows"
    assert _value(restored, "CHECKSUM TABLE test.t1") == expected, "restored chain checksum differs"

    print("Test: --help shows the new names and marks the old ones deprecated")
    _, out, _ = _xtrabackup(test_helper, ["--help"], "help")
    for old, new in (old_backup, old_prepare):
        assert re.search(rf"^\s+--{new}=name", out, re.MULTILINE), f"--help does not list --{new}"
        assert re.search(rf"--{old}=name\s+\(deprecated\)", out), f"--help does not mark --{old} deprecated"


# ============================================================================
# PXB-3788: options the runtime ignores must say so at startup
# ============================================================================

def test_deprecated_options_warn(test_helper):
    """PXB-3788: every option that no longer has an effect is still accepted
    but warns once at startup, saying why; --rsync, which still works, is
    announced as going away rather than as having no effect; a run passing
    none of them stays quiet.
    """
    test_helper.require_pxb_fix("PXB-3788", FIXED_IN_8_4_0_8)
    test_helper.initialize_db()
    _create_table(test_helper, 1, 3)

    print("Test: a backup passing no deprecated option prints no deprecation warning")
    full = os.path.join(test_helper.backup_dir, "full")
    out = _backup(test_helper, full, "full_backup")
    assert "is deprecated and has no effect" not in out, "deprecation warning for an option that was not passed"
    assert "--rsync is deprecated" not in out, "--rsync warning when --rsync was not passed"

    if shutil.which("rsync"):
        print("Test: --rsync still works and warns that it is going away")
        out = _backup(test_helper, os.path.join(test_helper.backup_dir, "rsync"), "rsync_backup", ["--rsync"])
        assert out.count("--rsync is deprecated and unsupported") == 1, "expected one --rsync deprecation warning"
        assert "shorten the FLUSH TABLES WITH READ LOCK window" in out, "--rsync warning does not say what it did"
        assert "--rsync is deprecated and has no effect" not in out, "--rsync still works; it must not say 'no effect'"
    else:
        print("rsync is not installed - skipping the --rsync part")

    # These are never read, so the mode does not matter; --prepare also shows
    # --no-version-check now warns outside --backup.
    dead = {"log": "--log", "innodb": "--innodb", "create-ib-logfile": "--create-ib-logfile",
            "rebuild_threads": "--rebuild_threads=4"}
    _, help_out, _ = _xtrabackup(test_helper, ["--help"], "help")
    if "--no-version-check" in help_out:  # only in -DWITH_VERSION_CHECK=ON builds
        dead["no-version-check"] = "--no-version-check"

    print(f"Test: {', '.join(dead.values())} are accepted and each warns once")
    out = _prepare(test_helper, full, "prepare_dead_options", list(dead.values()))
    for name in dead:
        warning = f"--{name} is deprecated and has no effect."
        assert out.count(warning) == 1, f"expected one '{warning}' warning, got {out.count(warning)}"
    assert "--create-ib-logfile is deprecated and has no effect. --prepare no longer creates redo log files." in out, (
        "--create-ib-logfile warning does not say why it has no effect"
    )


# ============================================================================
# PXB-2559: --history can write its record to another server
# ============================================================================

def test_history_on_other_server(test_helper):
    """PXB-2559: with --history-host/port/user/password the history record
    lands on that server, not the one backed up; an incremental reads its
    base LSN from there; the history connection is validated before the
    backup starts; --check-privileges checks the history account; and
    --history-ssl-mode applies to the history connection.
    """
    test_helper.require_pxb_fix("PXB-2559", FIXED_IN_8_4_0_8)
    test_helper.initialize_db()
    _create_table(test_helper, 1, 100)

    history = test_helper.create_replica(name="history", server_id=2, port=HISTORY_PORT)
    history.initialize_datadir()
    history.start()

    password = "historyPwd123"

    def history_count() -> str:
        return _value(history, "SELECT COUNT(*) FROM PERCONA_SCHEMA.xtrabackup_history")

    def grant(user: str, privileges: str, ssl: bool = False) -> None:
        history.mysql(f"CREATE USER {user}@'%' IDENTIFIED BY '{password}'{' REQUIRE SSL' if ssl else ''}")
        history.mysql(f"GRANT {privileges} ON PERCONA_SCHEMA.* TO {user}@'%'")
        # Opening any connection reads this table.
        history.mysql(f"GRANT SELECT ON performance_schema.replication_group_members TO {user}@'%'")

    grant("history_user", "CREATE, ALTER, INSERT, SELECT")

    def dsn(user: str = "history_user", pwd: str = password) -> list:
        return ["--history-host=127.0.0.1", f"--history-port={HISTORY_PORT}",
                f"--history-user={user}", f"--history-password={pwd}"]

    print("Test: the history record lands on the history server")
    full = os.path.join(test_helper.backup_dir, "full")
    _backup(test_helper, full, "full_backup", ["--history=full", *dsn()])
    assert history_count() == "1", "expected one history record on the history server"
    assert _value(history, "SELECT name FROM PERCONA_SCHEMA.xtrabackup_history") == "full"
    assert not _value(test_helper.primary, "SHOW DATABASES LIKE 'PERCONA_SCHEMA'"), (
        "PERCONA_SCHEMA was created on the server being backed up"
    )

    print("Test: an incremental reads its base LSN from the history server")
    to_lsn = _value(history, "SELECT innodb_to_lsn FROM PERCONA_SCHEMA.xtrabackup_history WHERE name = 'full'")
    assert to_lsn and to_lsn not in ("NULL", "0"), f"history record has no usable innodb_to_lsn: '{to_lsn}'"
    _create_table(test_helper, 101, 200)
    inc = os.path.join(test_helper.backup_dir, "inc")
    out = _backup(test_helper, inc, "inc_backup", ["--incremental-history-name=full", *dsn()])
    assert f"Found and using lsn: {to_lsn}" in out, "incremental did not take its LSN from the history server"
    assert _checkpoint(inc, "from_lsn") == to_lsn, "incremental did not start from the recorded LSN"
    assert history_count() == "1", "an incremental without --history added a history record"

    print("Test: history options without --history are rejected")
    rc, _, log_file = _xtrabackup(
        test_helper, _backup_args(test_helper, os.path.join(test_helper.backup_dir, "no_history")) + dsn(),
        "no_history_backup",
    )
    assert rc != 0, f"--history-host without --history was accepted. Log: {log_file}"

    print("Test: the history connection is validated before the backup starts")
    bad = os.path.join(test_helper.backup_dir, "bad_credentials")
    rc, _, log_file = _xtrabackup(
        test_helper, _backup_args(test_helper, bad) + ["--history=bad", *dsn(pwd="wrongPassword")],
        "bad_credentials_backup",
    )
    assert rc != 0, f"backup with a bad history password succeeded. Log: {log_file}"
    assert not os.path.exists(os.path.join(bad, "ibdata1")), "the backup ran before the history connection failed"

    print("Test: --check-privileges checks the history account")
    grant("noinsert", "CREATE, ALTER, SELECT")
    rc, out, log_file = _xtrabackup(
        test_helper,
        _backup_args(test_helper, os.path.join(test_helper.backup_dir, "noinsert"))
        + ["--check-privileges", "--history=no_insert", *dsn(user="noinsert")],
        "noinsert_backup",
    )
    assert rc != 0, f"--check-privileges accepted a history account without INSERT. Log: {log_file}"
    assert "missing required privilege INSERT on PERCONA_SCHEMA.xtrabackup_history" in out, (
        f"--check-privileges did not report the missing INSERT on the history server. Log: {log_file}"
    )

    print("Test: --history-ssl-mode applies to the history connection")
    grant("ssluser", "CREATE, ALTER, INSERT, SELECT", ssl=True)
    _backup(test_helper, os.path.join(test_helper.backup_dir, "tls"), "tls_backup",
            ["--history=tls", *dsn(user="ssluser")])
    assert history_count() == "2", "a REQUIRE SSL history account could not write over the default TLS connection"
    rc, _, log_file = _xtrabackup(
        test_helper,
        _backup_args(test_helper, os.path.join(test_helper.backup_dir, "notls"))
        + ["--history=no_tls", *dsn(user="ssluser"), "--history-ssl-mode=DISABLED"],
        "notls_backup",
    )
    assert rc != 0, f"--history-ssl-mode=DISABLED was ignored by the history connection. Log: {log_file}"
    assert history_count() == "2", "a history record was written with TLS disabled for a REQUIRE SSL account"
