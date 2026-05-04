#!/usr/bin/env python3
"""Python port of ``upgrade_backup_tests.sh``.

This module verifies backup/prepare/restore compatibility between two
Percona XtraBackup versions: a *previous* PXB build and a *current* PXB
build.  Each test takes one or more backups using the previous-version
xtrabackup binary (or a mix of previous + current) and then prepares
and restores them with the current-version xtrabackup binary, asserts
data integrity via ``CHECK TABLE`` and a row-count comparison, and
optionally re-applies the binlog from the original datadir.

Assumption: PS/MS and both PXB versions are already installed as tarballs.

Environment variables:

- ``TEST_BASE_DIR`` (default ``$HOME/upgrade_backup_tests``) - base directory
  for datadirs, sockets, backups and logs (inherited from :mod:`test_helper`).
- ``XTRABACKUP_DIR`` - path to the *current* xtrabackup ``bin`` directory.
- ``PREVIOUS_XTRABACKUP_DIR`` (default ``$HOME/pxb_previous/bin``) -
  path to the *previous* xtrabackup ``bin`` directory.
- ``MYSQLDIR`` - path to MySQL/PS install dir.
- ``QASCRIPTS`` - path to the ``server-qa`` checkout.
- ``ROCKSDB`` (``enabled`` | ``disabled``, default ``disabled``) - when
  enabled, also creates and loads ``test_rocksdb`` (PS 8.0+ only).
"""

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pytest

# Default TEST_BASE_DIR for this suite must be set before importing
# test_helper (which reads the env var at import time).
os.environ.setdefault(
    "TEST_BASE_DIR",
    os.path.join(os.path.expanduser("~"), "upgrade_backup_tests"),
)

# pylint: disable=wrong-import-position
from test_helper import BackupTestHelper, TEST_BASE_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")

PREVIOUS_XTRABACKUP_DIR = os.environ.get(
    "PREVIOUS_XTRABACKUP_DIR", os.path.join(HOME, "pxb_previous/bin")
)

ROCKSDB = os.environ.get("ROCKSDB", "disabled")


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def test_helper(request):
    """Per-test :class:`BackupTestHelper` with automatic cleanup."""
    test_name = request.node.name if hasattr(request, "node") else None
    helper = BackupTestHelper(test_name=test_name)
    # All upgrade tests use sysbench-style data load (mirrors the bash script).
    helper.load_tool = "sysbench"
    helper.server_version, helper.server_version_normalized = helper.get_mysql_version()
    helper.check_pt_checksum()
    helper.check_dependencies()
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

@contextmanager
def _use_xtrabackup(helper: BackupTestHelper, alt_dir: str):
    """Temporarily swap ``helper.xtrabackup_dir`` to ``alt_dir``.

    Used to take backups with the previous-version xtrabackup binary while
    preparing/restoring with the current-version binary, without changing
    any signatures in :mod:`test_helper`.
    """
    saved = helper.xtrabackup_dir
    helper.xtrabackup_dir = alt_dir
    try:
        yield
    finally:
        helper.xtrabackup_dir = saved


def _reset_backup_dir(helper: BackupTestHelper) -> None:
    """Recreate ``backup_dir`` from scratch (mirrors bash ``take_full_backup`` pre-step)."""
    if os.path.exists(helper.backup_dir):
        shutil.rmtree(helper.backup_dir)
    os.makedirs(helper.backup_dir, exist_ok=True)


def _run_sysbench_load(
    helper: BackupTestHelper,
    *,
    database: str = "test",
    time_sec: int = 20,
    threads: int = 50,
    engine: Optional[str] = None,
) -> None:
    """Run a background ``sysbench oltp_insert`` against ``helper.primary``."""
    cmd = [
        "sysbench",
        "/usr/share/sysbench/oltp_insert.lua",
        f"--tables={helper.num_tables}",
        f"--mysql-db={database}",
        "--mysql-user=root",
        f"--threads={threads}",
        "--db-driver=mysql",
        f"--mysql-socket={helper.socket_path}",
        f"--time={time_sec}",
        f"--rand-type={helper.random_type}",
    ]
    if engine:
        cmd.append(f"--mysql-storage-engine={engine}")
    cmd.append("run")

    log_file = os.path.join(helper.logdir, f"sysbench_{database}.log")
    log_fh = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
    subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)


def _prepare_restore_backup(
    helper: BackupTestHelper,
    prepare_params: str,
    restore_params: str,
    mysqld_options: str,
    backup_type: str,
    log_date: str,
) -> None:
    """Port of bash ``prepare_restore_backup``.

    Steps (matches the bash script):
      1. Prepare the full backup (with ``--apply-log-only`` when ``backup_type``
         is ``incremental``, plain ``--prepare`` otherwise).
      2. If ``backup_type == 'incremental'``: also prepare with
         ``--incremental-dir=<backup_dir>/inc1`` against the full backup.
      3. Capture row counts before stopping the server.
      4. Stop the primary, move ``datadir`` to a sibling ``data_orig_<ts>``.
      5. ``xtrabackup --copy-back`` the prepared full backup into ``datadir``.
      6. Start the primary with ``mysqld_options``.
      7. Optionally apply ``mysqlbinlog`` from the saved ``data_orig`` using
         the ``xtrabackup_binlog_info`` coordinates from the prepared full
         backup (skipped when binlog is encrypted or ``--skip-log-bin``).
      8. Run ``CHECK TABLE`` on the restored ``test`` database.
      9. Re-collect row counts; print a warning (non-fatal) on mismatch.
    """
    # --- Step 1 / 2: prepare ------------------------------------------------
    if backup_type == "incremental":
        helper.prepare_full_backup(prepare_params + " --apply-log-only", log_date)

        print("=>Preparing incremental backup")
        full_target = os.path.join(helper.backup_dir, "full")
        inc_target = os.path.join(helper.backup_dir, "inc1")
        # pylint: disable=protected-access
        cmd = helper._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--user=root",
            "--password=",
            "--prepare",
            f"--target-dir={full_target}",
            f"--incremental-dir={inc_target}",
        ] + prepare_params.split()
        log_file = os.path.join(helper.logdir, f"prepare_inc_backup_{log_date}_log")
        result = helper.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(
                f"ERR: Prepare of incremental backup failed. "
                f"Please check the log at: {log_file}"
            )
        print(
            "..Prepare of incremental backup was successful. "
            f"Logs available at: {log_file}"
        )
    else:
        helper.prepare_full_backup(prepare_params, log_date)

    # --- Step 3: snapshot row counts before restore -------------------------
    print("=>Collecting data before restore")
    orig_data = helper.count_rows()

    # --- Step 4: stop server, rename datadir --------------------------------
    print("=>Stopping mysql server and moving data directory")
    helper.primary.stop()

    data_orig = os.path.join(
        os.path.dirname(helper.datadir),
        f"data_orig_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    if os.path.exists(data_orig):
        shutil.rmtree(data_orig)
    shutil.move(helper.datadir, data_orig)

    # --- Step 5: copy-back full backup --------------------------------------
    helper.restore_backup_to(helper.datadir, restore_params, log_date)

    # --- Step 6: start primary with the requested mysqld options ------------
    helper.primary.mysqld_options = mysqld_options
    helper.primary.start()
    print("..The mysql server was started successfully")

    # --- Step 7: optionally apply binlog ------------------------------------
    skip_binlog_apply = (
        "binlog-encryption" in mysqld_options
        or "encrypt-binlog" in mysqld_options
        or "skip-log-bin" in mysqld_options
    )
    if not skip_binlog_apply:
        binlog_info_file = os.path.join(helper.backup_dir, "full", "xtrabackup_binlog_info")
        if os.path.exists(binlog_info_file):
            with open(binlog_info_file, "r", encoding="utf-8") as f:
                line = f.readline().strip()
            parts = line.split()
            xb_binlog_file = parts[0] if parts else ""
            xb_binlog_pos = parts[1] if len(parts) > 1 else ""
            print(
                f"Xtrabackup binlog position: {xb_binlog_file}, {xb_binlog_pos}"
            )

            binlog_path = os.path.join(data_orig, xb_binlog_file)
            if xb_binlog_file and os.path.exists(binlog_path):
                print(
                    f"=>Applying binlog to restored data starting from "
                    f"{xb_binlog_file}, {xb_binlog_pos}"
                )
                mysqlbinlog = subprocess.Popen(
                    [
                        os.path.join(helper.mysqldir, "bin/mysqlbinlog"),
                        binlog_path,
                        f"--start-position={xb_binlog_pos}",
                    ],
                    stdout=subprocess.PIPE,
                )
                mysql = subprocess.Popen(
                    [
                        os.path.join(helper.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{helper.socket_path}",
                    ],
                    stdin=mysqlbinlog.stdout,
                )
                mysqlbinlog.stdout.close()
                mysql.communicate()
                if mysql.returncode != 0:
                    print("ERR: The binlog could not be applied to the restored data")
                time.sleep(5)

    # --- Step 8: integrity check --------------------------------------------
    print("=>Checking the restored data")
    helper.check_tables()

    # --- Step 9: row-count comparison (non-fatal, mirrors bash) -------------
    res_data = helper.count_rows()
    if orig_data != res_data:
        print("ERR: Data changed after restore.")
        print(f"Original data:\n{orig_data}")
        print(f"Restored data:\n{res_data}")
    else:
        print("Restored data is correct")


def _build_encrypt_options(helper: BackupTestHelper) -> str:
    """Return mysqld options for encrypted upgrade tests, matching the bash logic.

    Branch selection mirrors ``test_upgrade_backup_encrypt`` in the bash
    script: presence of "8.0" and "MySQL Community Server" in the version
    string drives MS 8.0 vs PS 8.0 vs the 5.7 fallback path.
    """
    keyring = os.path.join(helper.mysqldir, "keyring")
    version_str = helper.server_version or ""

    is_8x = "8.0" in version_str
    is_ms = helper.server_type == "MS"

    if is_8x and is_ms:
        # MS 8.0
        return (
            "--early-plugin-load=keyring_file.so "
            f"--keyring_file_data={keyring} "
            "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
            "--default-table-encryption=ON --log-slave-updates "
            "--gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
            "--master_verify_checksum=ON --binlog_checksum=CRC32 "
            "--binlog-rotate-encryption-master-key-at-startup "
            "--table-encryption-privilege-check=ON"
        )
    if is_8x:
        # PS 8.0
        return (
            "--early-plugin-load=keyring_file.so "
            f"--keyring_file_data={keyring} "
            "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
            "--default-table-encryption=ON "
            "--innodb_encrypt_online_alter_logs=ON "
            "--innodb_temp_tablespace_encrypt=ON --log-slave-updates "
            "--gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
            "--master_verify_checksum=ON --binlog_checksum=CRC32 "
            "--encrypt-tmp-files --innodb_sys_tablespace_encrypt "
            "--binlog-rotate-encryption-master-key-at-startup "
            "--table-encryption-privilege-check=ON"
        )
    # PS/MS 5.7 fallback (also reached by 8.4+; matches the bash logic)
    return (
        "--log-bin=binlog --early-plugin-load=keyring_file.so "
        f"--keyring_file_data={keyring} --log-slave-updates --gtid-mode=ON "
        "--enforce-gtid-consistency --binlog-format=row "
        "--master_verify_checksum=ON --binlog_checksum=CRC32"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_upgrade_full_backup(test_helper):  # pylint: disable=redefined-outer-name
    """Upgrade test: full backup with previous PXB; prepare/restore with current PXB."""
    helper = test_helper
    helper.mysqld_options = "--log-bin=binlog"
    helper.backup_params = ""
    helper.prepare_params = ""
    helper.restore_params = ""

    print("\n==> Test: Full backup and restore")
    print("Full backup using previous xtrabackup version and prepare/restore using current xtrabackup version")

    helper.initialize_db(rocksdb=(ROCKSDB == "enabled"))

    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=20)
    if ROCKSDB == "enabled":
        _run_sysbench_load(
            helper, database="test_rocksdb", time_sec=20, engine="ROCKSDB"
        )

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)

    _prepare_restore_backup(
        helper,
        prepare_params="",
        restore_params="",
        mysqld_options="--log-bin=binlog",
        backup_type="full",
        log_date=log_date,
    )


def test_upgrade_inc_backup(test_helper):  # pylint: disable=redefined-outer-name
    """Upgrade test: incremental backup combinations across previous + current PXB.

    Runs two scenarios sequentially:
      A. full + inc with previous PXB; prepare/restore with current PXB.
      B. full with previous PXB, inc with current PXB; prepare/restore current PXB.
    """
    helper = test_helper
    helper.mysqld_options = "--log-bin=binlog"
    helper.backup_params = ""
    helper.prepare_params = ""
    helper.restore_params = ""

    # The bash script relies on ``test_upgrade_full_backup`` having already
    # initialised the database; in pytest each test owns its own datadir,
    # so we (re)initialise here.
    helper.initialize_db(rocksdb=(ROCKSDB == "enabled"))

    # ---- Scenario A: full+inc with previous PXB -----------------------------
    print("\n==> Test: Full, Incremental backup using previous xtrabackup version "
          "and prepare/restore using current xtrabackup version")
    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=30)
    if ROCKSDB == "enabled":
        _run_sysbench_load(
            helper, database="test_rocksdb", time_sec=30, engine="ROCKSDB"
        )

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)
        helper.take_incremental_backup(
            1, os.path.join(helper.backup_dir, "full"), log_date
        )

    _prepare_restore_backup(
        helper,
        prepare_params="",
        restore_params="",
        mysqld_options="--log-bin=binlog",
        backup_type="incremental",
        log_date=log_date,
    )

    print("\n###################################################################################")

    # ---- Scenario B: full with previous PXB, inc with current PXB -----------
    print("\n==> Test: Full backup using previous xtrabackup version, "
          "incremental backup and prepare/restore using current xtrabackup version")
    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=30)
    if ROCKSDB == "enabled":
        _run_sysbench_load(
            helper, database="test_rocksdb", time_sec=30, engine="ROCKSDB"
        )

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)
    helper.take_incremental_backup(
        1, os.path.join(helper.backup_dir, "full"), log_date
    )

    _prepare_restore_backup(
        helper,
        prepare_params="",
        restore_params="",
        mysqld_options="--log-bin=binlog",
        backup_type="incremental",
        log_date=log_date,
    )


def test_upgrade_backup_encrypt(test_helper):  # pylint: disable=redefined-outer-name
    """Upgrade test: encrypted backups across previous + current PXB.

    Runs three scenarios sequentially against the same encrypted database:
      A. full backup with previous PXB; prepare/restore with current PXB.
      B. full + inc with previous PXB; prepare/restore with current PXB.
      C. full with previous PXB, inc with current PXB; prepare/restore current PXB.

    All scenarios use ``keyring_file`` plugin with per-binary
    ``--xtrabackup-plugin-dir`` so that the previous PXB resolves its plugin
    relative to ``PREVIOUS_XTRABACKUP_DIR`` and the current PXB uses
    ``XTRABACKUP_DIR``.
    """
    helper = test_helper

    server_options = _build_encrypt_options(helper)
    keyring = os.path.join(helper.mysqldir, "keyring")
    prev_plugin_params = (
        f"--keyring_file_data={keyring} "
        f"--xtrabackup-plugin-dir={PREVIOUS_XTRABACKUP_DIR}/../lib/plugin"
    )
    curr_plugin_params = (
        f"--keyring_file_data={keyring} "
        f"--xtrabackup-plugin-dir={helper.xtrabackup_dir}/../lib/plugin"
    )

    helper.mysqld_options = server_options
    helper.prepare_params = curr_plugin_params
    helper.restore_params = curr_plugin_params

    print("\n==> Full backup and restore with encryption")
    print("Test: Full backup using previous xtrabackup version and "
          "prepare/restore using current xtrabackup version")
    helper.initialize_db()

    # ---- Scenario A: full backup with previous PXB --------------------------
    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=20)

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    helper.backup_params = prev_plugin_params
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)

    _prepare_restore_backup(
        helper,
        prepare_params=curr_plugin_params,
        restore_params=curr_plugin_params,
        mysqld_options=server_options,
        backup_type="full",
        log_date=log_date,
    )

    print("\n###################################################################################")

    # ---- Scenario B: full+inc with previous PXB ----------------------------
    print("\n==> Incremental backup and restore with encryption")
    print("Test: Full, Incremental backup using previous xtrabackup version "
          "and prepare/restore using current xtrabackup version")
    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=30)

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    helper.backup_params = prev_plugin_params
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)
        helper.take_incremental_backup(
            1, os.path.join(helper.backup_dir, "full"), log_date
        )

    _prepare_restore_backup(
        helper,
        prepare_params=curr_plugin_params,
        restore_params=curr_plugin_params,
        mysqld_options=server_options,
        backup_type="incremental",
        log_date=log_date,
    )

    print("\n###################################################################################")

    # ---- Scenario C: full prev, inc current --------------------------------
    print("\n==> Test: Full backup using previous xtrabackup version, "
          "incremental backup and prepare/restore using current xtrabackup version")
    print("=>Run a load")
    _run_sysbench_load(helper, database="test", time_sec=30)

    log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    _reset_backup_dir(helper)
    helper.backup_params = prev_plugin_params
    with _use_xtrabackup(helper, PREVIOUS_XTRABACKUP_DIR):
        helper.take_full_backup(log_date)
    helper.backup_params = curr_plugin_params
    helper.take_incremental_backup(
        1, os.path.join(helper.backup_dir, "full"), log_date
    )

    _prepare_restore_backup(
        helper,
        prepare_params=curr_plugin_params,
        restore_params=curr_plugin_params,
        mysqld_options=server_options,
        backup_type="incremental",
        log_date=log_date,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="PXB Upgrade Backup Tests (previous + current PXB)"
    )
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=["Full_backup", "Inc_backup", "Encryption", "All"],
        help="Test suites to run",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output"
    )

    args = parser.parse_args()

    if not args.test_suites:
        print(
            "This script tests backup/prepare/restore compatibility between two PXB versions"
        )
        print("Assumption: PS/MS and both PXB versions are already installed as tarballs")
        print("Usage:")
        print("1. Set environment variables (or use defaults):")
        print(f"   export TEST_BASE_DIR={TEST_BASE_DIR}")
        print("   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-current/bin")
        print(f"   export PREVIOUS_XTRABACKUP_DIR={PREVIOUS_XTRABACKUP_DIR}")
        print("   export MYSQLDIR=$HOME/Percona-Server-8.0.x-Linux.x86_64.glibc2.35")
        print("   export QASCRIPTS=$HOME/server-qa")
        print(f"   export ROCKSDB={ROCKSDB}    # 'enabled' to also load test_rocksdb")
        print("2. Run the script as: pytest upgrade_backup_tests.py -k <test_name> -s -v")
        print("   Or: python upgrade_backup_tests.py <Test Suites>")
        print("   Test Suites:")
        print("   Full_backup")
        print("   Inc_backup")
        print("   Encryption")
        print("   All")
        print("")
        print(f"3. Logs are available under: {TEST_BASE_DIR} (test-specific directories)")
        sys.exit(1)

    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    test_mapping = {
        "Full_backup": ["test_upgrade_full_backup"],
        "Inc_backup": ["test_upgrade_inc_backup"],
        "Encryption": ["test_upgrade_backup_encrypt"],
    }

    selected_tests = []
    if "All" in args.test_suites:
        # Run everything; no -k filter required
        selected_tests = []
    else:
        for suite in args.test_suites:
            if suite in test_mapping:
                selected_tests.extend(test_mapping[suite])

    if selected_tests:
        k_expr = " or ".join(selected_tests)
        pytest_args.extend(["-k", k_expr])

    pytest.main(pytest_args)
