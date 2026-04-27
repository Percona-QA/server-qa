#!/usr/bin/env python3
"""Python port of ``replication_backup_tests.sh``.

This module runs replication + backup tests against MySQL / Percona Server,
using Percona XtraBackup.  Each test exercises one ``mysqld`` option
combination (GTID / no-GTID, multi / single-threaded replica, encryption)
and, within that, runs both bash sub-scenarios:

    1. Create ``replica1`` from a backup taken on the *primary*.
    2. Create ``replica2`` from a backup taken on *replica1*.

Primary and replicas all share ``MYSQLDIR`` as their mysqld ``--basedir``;
only ``--datadir`` / ``--socket`` / ``--port`` / ``--server-id`` differ per
instance, so no mysql tarball copying is required.

Environment variables (inherited from :mod:`test_helper`):

- ``TEST_BASE_DIR`` (default ``$HOME/replication_backup_tests``) — base dir
  for all datadirs, sockets, backups and logs.
- ``XTRABACKUP_DIR`` — path to xtrabackup install ``bin`` directory.
- ``MYSQLDIR`` — path to MySQL/PS install dir.
- ``QASCRIPTS`` — path to server-qa checkout.
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional, Tuple

import pytest

# Ensure TEST_BASE_DIR defaults to a dedicated per-suite directory BEFORE
# importing test_helper (which reads the env var at import time).
os.environ.setdefault(
    "TEST_BASE_DIR",
    os.path.join(os.path.expanduser("~"), "replication_backup_tests"),
)

# pylint: disable=wrong-import-position
from test_helper import (  # noqa: E402
    BackupTestHelper,
    TEST_BASE_DIR,
    MySQLServer,
)

# ---------------------------------------------------------------------------
# Module-level dependency check (ports bash ``check_dependencies``)
# ---------------------------------------------------------------------------

_MISSING_DEPS = []
if subprocess.run(["which", "sysbench"], capture_output=True, check=False).returncode != 0:
    _MISSING_DEPS.append("sysbench")
if subprocess.run(
    ["which", "pt-table-checksum"], capture_output=True, check=False
).returncode != 0:
    _MISSING_DEPS.append("pt-table-checksum")

if _MISSING_DEPS:
    pytest.skip(
        f"Missing required dependencies: {', '.join(_MISSING_DEPS)}. "
        "Install sysbench + percona-toolkit to run replication_backup_tests.",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Option constants
# ---------------------------------------------------------------------------

GTID_OPTIONS = (
    "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency "
    "--binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"
)

NO_GTID_OPTIONS = "--log-bin=binlog --log-slave-updates"


def _encrypt_options_8(keyring_path: str) -> str:
    """Encryption-enabling mysqld options for MySQL/PS 8.0+."""
    return (
        "--early-plugin-load=keyring_file.so "
        f"--keyring_file_data={keyring_path} "
        "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
        "--default-table-encryption=ON --log-slave-updates --gtid-mode=ON "
        "--enforce-gtid-consistency --binlog-format=row "
        "--master_verify_checksum=ON --binlog_checksum=CRC32 "
        "--binlog-rotate-encryption-master-key-at-startup "
        "--table-encryption-privilege-check=ON"
    )


def _encrypt_options_57(keyring_path: str) -> str:
    """Encryption-enabling mysqld options for MySQL/PS 5.7."""
    return (
        "--early-plugin-load=keyring_file.so "
        f"--keyring_file_data={keyring_path} "
        "--innodb-encrypt-tables=ON --encrypt-binlog --encrypt-tmp-files "
        "--innodb-temp-tablespace-encrypt "
        "--innodb-encrypt-online-alter-logs=ON --log-slave-updates "
        "--gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
        "--master_verify_checksum=ON --binlog_checksum=CRC32"
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def test_helper(request):
    """Per-test :class:`BackupTestHelper` with automatic cleanup."""
    test_name = request.node.name if hasattr(request, "node") else None
    helper = BackupTestHelper(test_name=test_name)
    helper.version, helper.version_normalized = helper.get_mysql_version()
    helper.check_pt_checksum()
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir(test_helper):  # pylint: disable=redefined-outer-name
    """Ensure ``logdir`` exists before each test."""
    os.makedirs(test_helper.logdir, exist_ok=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _xb_is_80(xtrabackup_dir: str) -> bool:
    """Return True when the installed xtrabackup is 8.0+."""
    try:
        result = subprocess.run(
            [os.path.join(xtrabackup_dir, "xtrabackup"), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = (result.stdout or "") + (result.stderr or "")
    return "8.0" in out or "9.0" in out or "9.1" in out


def _sysbench_load(
    helper: BackupTestHelper,
    server: MySQLServer,
    *,
    threads: int = 20,
    time_sec: int = 20,
    background: bool = False,
    log_name: str = "sysbench.log",
) -> None:
    """Run a sysbench ``oltp_insert`` workload against ``server``."""
    cmd = [
        "sysbench",
        "/usr/share/sysbench/oltp_insert.lua",
        f"--tables={helper.num_tables}",
        "--mysql-db=test",
        "--mysql-user=root",
        f"--threads={threads}",
        "--db-driver=mysql",
        f"--mysql-socket={server.socket_path}",
        f"--time={time_sec}",
        f"--rand-type={helper.random_type}",
        "run",
    ]
    log_file = os.path.join(helper.logdir, log_name)
    with open(log_file, "w", encoding="utf-8") as f:
        if background:
            subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
        else:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)


def _pt_table_checksum(helper: BackupTestHelper, source: MySQLServer) -> None:
    """Run ``pt-table-checksum`` against ``source`` (primary) for the ``test`` DB."""
    cmd = [
        "pt-table-checksum",
        f"S={source.socket_path},u=root",
        "-d",
        "test",
        "--recursion-method",
        "hosts",
        "--no-check-binlog-format",
        "--no-version-check",
    ]
    log_file = os.path.join(helper.logdir, f"pt_checksum_{source.name}.log")
    with open(log_file, "w", encoding="utf-8") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)


def _keyring_path(helper: BackupTestHelper) -> str:
    """Return the conventional path for a ``keyring_file`` keyring."""
    return os.path.join(helper.logdir, "keyring")


def _encrypt_params(helper: BackupTestHelper) -> Tuple[str, str]:
    """Return ``(backup_params, keyring_src)`` for encrypted tests."""
    keyring = _keyring_path(helper)
    plugin_dir = os.path.join(helper.xtrabackup_dir, "..", "lib", "plugin")
    params = f"--keyring_file_data={keyring} --xtrabackup-plugin-dir={plugin_dir}"
    return params, keyring


# ---------------------------------------------------------------------------
# Shared replication flow (ports bash ``replicate_primary``)
# ---------------------------------------------------------------------------

def _replicate_primary(
    helper: BackupTestHelper,
    *,
    mysqld_options: str,
    backup_params: str = "",
    prepare_params: str = "",
    restore_params: str = "",
    keyring_src: Optional[str] = None,
) -> None:
    """Run the full two-stage replication flow from ``replication_backup_tests.sh``.

    The primary must already be initialised and running (call
    ``helper.initialize_db()`` beforehand).  Both stages create a replica
    (``replica1`` from primary, then ``replica2`` from ``replica1``), start
    replication, verify IO/SQL threads, run ``check_tables`` on each replica
    and a ``pt-table-checksum`` on the primary.
    """
    helper.backup_params = backup_params
    helper.prepare_params = prepare_params
    helper.restore_params = restore_params

    primary = helper.primary

    # -- Stage 1: replica1 from primary -----------------------------------
    print("\n==> Stage 1: Create replica1 from backup of primary")
    print("..Running sysbench load on primary")
    _sysbench_load(helper, primary, threads=20, time_sec=30, background=True,
                   log_name="sysbench_primary_stage1.log")

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    helper.take_backup_from(primary, extra_args=None, log_date=log_date)
    helper.prepare_full_backup(prepare_params, log_date)

    replica1 = helper.create_replica(
        name="replica1",
        server_id=102,
        port=18615,
        mysqld_options=mysqld_options,
        keyring_src=keyring_src,
        tmpdir=None,
    )
    # Restore into the replica1 datadir (tmpdir is then set to the datadir).
    helper.restore_backup_to(replica1.datadir, restore_params, log_date)
    replica1.tmpdir = replica1.datadir
    # restore_backup_to wipes the datadir, so the keyring copy that
    # create_replica placed inside it is gone.  Re-copy here so mysqld can
    # find the master keys referenced by --keyring_file_data.
    if keyring_src and os.path.exists(keyring_src):
        _kr_dst = os.path.join(replica1.datadir, os.path.basename(keyring_src))
        if not os.path.exists(_kr_dst):
            import shutil as _shutil1
            _shutil1.copy(keyring_src, _kr_dst)
    # #region agent log
    import json as _json_a
    import time as _time_a
    _log_path_a = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug-f2a631.log")
    def _dbg_a(msg, data):
        try:
            with open(_log_path_a, "a") as _f:
                _f.write(_json_a.dumps({"sessionId":"f2a631","location":"replication_backup_tests.py:_replicate_primary","message":msg,"data":data,"timestamp":int(_time_a.time()*1000),"hypothesisId":"KEY"}) + "\n")
        except Exception:
            pass
    _kr_dst_a = os.path.join(replica1.datadir, os.path.basename(keyring_src)) if keyring_src else None
    _kr_src_size_a = os.path.getsize(keyring_src) if keyring_src and os.path.exists(keyring_src) else -1
    _kr_dst_size_a = os.path.getsize(_kr_dst_a) if _kr_dst_a and os.path.exists(_kr_dst_a) else -1
    try:
        _datadir_files_a = sorted(os.listdir(replica1.datadir))[:60]
    except Exception as _e_a:
        _datadir_files_a = [f"<err: {_e_a}>"]
    _dbg_a("Pre-start state for replica1", {
        "keyring_src": keyring_src,
        "keyring_src_exists": bool(keyring_src) and os.path.exists(keyring_src),
        "keyring_src_size": _kr_src_size_a,
        "keyring_dst_path": _kr_dst_a,
        "keyring_dst_exists": bool(_kr_dst_a) and os.path.exists(_kr_dst_a),
        "keyring_dst_size": _kr_dst_size_a,
        "replica1_mysqld_options": replica1.mysqld_options,
        "replica1_datadir_files_first60": _datadir_files_a,
    })
    # #endregion agent log
    replica1.start()
    helper.configure_replication(replica1, primary, slave_info=False)

    replica1.check_tables(database="test")
    _pt_table_checksum(helper, primary)

    # -- Stage 2: replica2 from replica1 ----------------------------------
    print("\n==> Stage 2: Create replica2 from backup of replica1")
    print("..Running sysbench load on primary")
    _sysbench_load(helper, primary, threads=20, time_sec=20, background=True,
                   log_name="sysbench_primary_stage2.log")

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")

    # If encryption is in use, rewrite keyring paths inside the backup/prepare
    # params to point at replica1's keyring (mirrors the bash sed).
    stage2_backup_params = backup_params
    stage2_prepare_params = prepare_params
    if keyring_src and "keyring_file" in mysqld_options:
        replica1_keyring = os.path.join(replica1.datadir, os.path.basename(keyring_src))
        stage2_backup_params = re.sub(
            r"--keyring_file_data=\S+",
            f"--keyring_file_data={replica1_keyring}",
            backup_params,
        )
        stage2_prepare_params = re.sub(
            r"--keyring_file_data=\S+",
            f"--keyring_file_data={replica1_keyring}",
            prepare_params,
        )

    helper.backup_params = stage2_backup_params
    helper.prepare_params = stage2_prepare_params

    # Choose --slave-info [+ --safe-slave-backup].
    #
    # Note: the original bash test gates --safe-slave-backup on
    # slave_parallel_workers >= 2 (MTS only), but runtime evidence shows that
    # even single-threaded non-GTID replicas suffer from a data/position race
    # during the stage-2 backup: while the replica's SQL thread keeps applying
    # events from the primary, xtrabackup's data copy picks up pages written by
    # those events, yet xtrabackup_slave_info captures an earlier
    # Exec_Master_Log_Pos. Replica2 then fails with
    # "Duplicate entry ... Error_code: 1062" because it re-applies already-
    # applied events.  --safe-slave-backup runs STOP REPLICA SQL_THREAD before
    # the backup so the captured slave-info position matches the data.
    # For GTID replicas, AUTO_POSITION=1 makes the exact file/pos irrelevant,
    # so --safe-slave-backup is not needed there.
    use_gtid = "gtid-mode=ON" in mysqld_options
    extra = ["--slave-info"]
    if not use_gtid:
        extra.append("--safe-slave-backup")

    helper.take_backup_from(replica1, extra_args=extra, log_date=log_date)
    helper.prepare_full_backup(stage2_prepare_params, log_date)

    replica2_keyring_src: Optional[str] = None
    if keyring_src and "keyring_file" in mysqld_options:
        candidate = os.path.join(replica1.datadir, os.path.basename(keyring_src))
        if os.path.exists(candidate):
            replica2_keyring_src = candidate

    replica2 = helper.create_replica(
        name="replica2",
        server_id=103,
        port=18620,
        mysqld_options=mysqld_options,
        keyring_src=replica2_keyring_src,
        tmpdir=None,
    )
    helper.restore_backup_to(replica2.datadir, restore_params, log_date)
    replica2.tmpdir = replica2.datadir
    # restore_backup_to wiped the datadir; re-copy keyring (see replica1 above).
    if replica2_keyring_src and os.path.exists(replica2_keyring_src):
        _kr_dst2 = os.path.join(replica2.datadir, os.path.basename(replica2_keyring_src))
        if not os.path.exists(_kr_dst2):
            import shutil as _shutil2
            _shutil2.copy(replica2_keyring_src, _kr_dst2)
    replica2.start(extra_args=["--skip-slave-start"])
    helper.configure_replication(replica2, primary, slave_info=True)

    replica2.check_tables(database="test")
    _pt_table_checksum(helper, primary)

    # Restore the (primary) backup_params so subsequent helper.cleanup() runs
    # see the originally-requested values.
    helper.backup_params = backup_params
    helper.prepare_params = prepare_params


# ---------------------------------------------------------------------------
# Tests (one per bash test config)
# ---------------------------------------------------------------------------

def test_replication_gtid_multithreaded(test_helper):  # pylint: disable=redefined-outer-name
    """Replication with GTID options + multithreaded replica (4 workers)."""
    opts = f"{GTID_OPTIONS} --slave-parallel-workers=4"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(test_helper, mysqld_options=opts)


def test_replication_gtid_singlethreaded(test_helper):  # pylint: disable=redefined-outer-name
    """Replication with GTID options + single-threaded replica."""
    workers = "1" if _xb_is_80(test_helper.xtrabackup_dir) else "0"
    opts = f"{GTID_OPTIONS} --slave-parallel-workers={workers}"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(test_helper, mysqld_options=opts)


def test_replication_nogtid_multithreaded(test_helper):  # pylint: disable=redefined-outer-name
    """Replication without GTID options + multithreaded replica (4 workers)."""
    opts = f"{NO_GTID_OPTIONS} --slave-parallel-workers=4"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(test_helper, mysqld_options=opts)


def test_replication_nogtid_singlethreaded(test_helper):  # pylint: disable=redefined-outer-name
    """Replication without GTID options + single-threaded replica."""
    opts = f"{NO_GTID_OPTIONS} --slave-parallel-workers=0"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(test_helper, mysqld_options=opts)


def test_replication_gtid_encryption(test_helper):  # pylint: disable=redefined-outer-name
    """Replication with GTID options + keyring_file encryption."""
    backup_params, keyring_src = _encrypt_params(test_helper)
    encrypt_opts = (
        _encrypt_options_8(keyring_src)
        if _xb_is_80(test_helper.xtrabackup_dir)
        else _encrypt_options_57(keyring_src)
    )
    opts = f"{GTID_OPTIONS} {encrypt_opts}"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(
        test_helper,
        mysqld_options=opts,
        backup_params=backup_params,
        prepare_params=backup_params,
        restore_params=backup_params,
        keyring_src=keyring_src,
    )


def test_replication_nogtid_encryption(test_helper):  # pylint: disable=redefined-outer-name
    """Replication without GTID options + keyring_file encryption."""
    backup_params, keyring_src = _encrypt_params(test_helper)
    encrypt_opts = (
        _encrypt_options_8(keyring_src)
        if _xb_is_80(test_helper.xtrabackup_dir)
        else _encrypt_options_57(keyring_src)
    )
    opts = f"{NO_GTID_OPTIONS} {encrypt_opts}"
    test_helper.mysqld_options = opts
    test_helper.initialize_db()
    _replicate_primary(
        test_helper,
        mysqld_options=opts,
        backup_params=backup_params,
        prepare_params=backup_params,
        restore_params=backup_params,
        keyring_src=keyring_src,
    )


# ---------------------------------------------------------------------------
# Command-line wrapper (mirrors inc_backup_load_tests.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PXB Replication Backup Tests")
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=["Gtid_tests", "NoGtid_tests", "Encryption_tests", "All"],
        help="Test suites to run",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if not args.test_suites:
        print("This script runs the replication + backup tests using PXB.")
        print("Assumption: PS/MySQL and PXB are already installed as tarballs;")
        print("            sysbench and percona-toolkit are installed.")
        print("Usage:")
        print("  export TEST_BASE_DIR=$HOME/replication_backup_tests")
        print("  export XTRABACKUP_DIR=$HOME/pxb-9.1/bld_9.1/install/bin")
        print("  export MYSQLDIR=$HOME/mysql-9.1/bld_9.1/install")
        print("  export QASCRIPTS=$HOME/server-qa")
        print("  pytest replication_backup_tests.py -k <test_name> -s -v")
        print("  python replication_backup_tests.py <Test Suites>")
        print("  Test Suites:")
        print("    Gtid_tests")
        print("    NoGtid_tests")
        print("    Encryption_tests")
        print("    All")
        print(f"Logs are available under: {TEST_BASE_DIR} (test-specific directories)")
        sys.exit(1)

    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    test_mapping = {
        "Gtid_tests": [
            "test_replication_gtid_multithreaded",
            "test_replication_gtid_singlethreaded",
        ],
        "NoGtid_tests": [
            "test_replication_nogtid_multithreaded",
            "test_replication_nogtid_singlethreaded",
        ],
        "Encryption_tests": [
            "test_replication_gtid_encryption",
            "test_replication_nogtid_encryption",
        ],
        "All": [
            "test_replication_gtid_multithreaded",
            "test_replication_gtid_singlethreaded",
            "test_replication_nogtid_multithreaded",
            "test_replication_nogtid_singlethreaded",
            "test_replication_gtid_encryption",
            "test_replication_nogtid_encryption",
        ],
    }

    selected = []
    for suite in args.test_suites:
        selected.extend(test_mapping.get(suite, []))

    if selected:
        pytest_args.extend(["-k", " or ".join(selected)])

    pytest.main(pytest_args)
