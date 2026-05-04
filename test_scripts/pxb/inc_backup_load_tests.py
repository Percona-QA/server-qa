#!/usr/bin/env python3
"""
This script tests backup with a load tool as pquery/pstress/sysbench
Assumption: PS and PXB are already installed as tarballs
"""

import os
import sys
import subprocess
import shutil
import pytest

from test_helper import BackupTestHelper, TEST_BASE_DIR, KMIP_CONFIGS, CORE_FILE_OPT


# Pytest fixtures and test functions
@pytest.fixture(scope="function")
def test_helper(request):
    """Create a test helper instance."""
    # Get the test name from the request
    test_name = request.node.name if hasattr(request, 'node') else None
    helper = BackupTestHelper(test_name=test_name)
    helper.server_version, helper.server_version_normalized = helper.get_mysql_version()
    helper.check_pt_checksum()
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir(test_helper):
    """Setup log directory."""
    if not os.path.exists(test_helper.logdir):
        os.makedirs(test_helper.logdir)
    pstress_logdir = os.path.join(test_helper.logdir, "pstress")
    if os.path.exists(pstress_logdir):
        shutil.rmtree(pstress_logdir)
    os.makedirs(pstress_logdir)


# Test functions
def test_normal_backup(test_helper):
    """Test normal incremental backup and restore."""
    test_helper.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    tool_options = f"--tables {test_helper.num_tables} --records {test_helper.table_size} --threads {test_helper.threads} --seconds {test_helper.seconds} --no-encryption --undo-tbs-sql 0"
    if test_helper.server_type == "MS":
        tool_options += " --no-column-compression --no-temp-tables"

    test_helper.initialize_db()
    test_helper.run_load(tool_options)
    test_helper.take_backup()
    test_helper.check_tables()


def test_memory_estimation_backup(test_helper):
    """Test incremental backup and restore with memory estimation (sysbench only)."""
    if not test_helper.server_version or not test_helper.server_version_normalized:
        test_helper.server_version, test_helper.server_version_normalized = test_helper.get_mysql_version()
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Memory estimation is not supported in PXB 2.4 (5.7), skipping tests")

    original_tool = test_helper.load_tool
    test_helper.load_tool = "sysbench"
    try:
        test_helper.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
        test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
        test_helper.prepare_params = f"{CORE_FILE_OPT} --use-free-memory-pct=20"
        test_helper.restore_params = ""

        test_helper.initialize_db()
        test_helper.run_load("")
        test_helper.take_backup()
        test_helper.check_tables()
    finally:
        test_helper.load_tool = original_tool


@pytest.mark.parametrize("page_tracking", [False, True], ids=["no_pt", "pt"])
def test_keyring_plugin_backup(test_helper, page_tracking):
    """Test backup with keyring_file plugin, page-tracking on/off."""
    test_helper.run_keyring_plugin_backup(page_tracking=page_tracking)


@pytest.mark.parametrize("page_tracking", [False, True], ids=["no_pt", "pt"])
def test_keyring_component_backup(test_helper, page_tracking):
    """Test backup with keyring_file component, page-tracking on/off."""
    test_helper.run_keyring_component_backup(page_tracking=page_tracking)


def test_rocksdb_backup(test_helper):
    """Test backup with RocksDB."""
    # Ensure version and server type are detected (same pattern as version-only checks)
    if not test_helper.server_version or not test_helper.server_version_normalized:
        test_helper.server_version, test_helper.server_version_normalized = test_helper.get_mysql_version()
    if not test_helper.server_type:
        test_helper.get_mysql_type()

    if test_helper.server_version_normalized < 80000:
        pytest.skip("Rocksdb backup is not supported in MS/PS 5.7")
    if test_helper.server_type == "MS":
        pytest.skip("RocksDB is unsupported in MS")

    test_helper.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    tool_options = f"--tables {test_helper.num_tables} --records {test_helper.table_size} --threads {test_helper.threads} --seconds {test_helper.seconds} --no-encryption --engine=rocksdb"

    test_helper.initialize_db()
    subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/ps-admin"), "--enable-rocksdb", "-uroot", f"-S{test_helper.socket_path}"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/mysql"), "-uroot", f"-S{test_helper.socket_path}", "-e", "CREATE DATABASE IF NOT EXISTS test"],
        check=False,
    )
    test_helper.run_load(tool_options)
    test_helper.take_backup()
    test_helper.check_tables()


def test_page_tracking_backup(test_helper):
    """Test backup with page tracking."""
    # Ensure version is detected (same pattern as other version checks)
    if not test_helper.server_version or not test_helper.server_version_normalized:
        test_helper.server_version, test_helper.server_version_normalized = test_helper.get_mysql_version()

    if test_helper.server_version_normalized < 80000:
        pytest.skip("Page Tracking is not supported in MS/PS 5.7")

    test_helper.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl} --page-tracking"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    tool_options = f"--tables {test_helper.num_tables} --records {test_helper.table_size} --threads {test_helper.threads} --seconds {test_helper.seconds} --no-encryption --undo-tbs-sql 0"
    if test_helper.server_type == "MS":
        tool_options += " --no-column-compression --no-temp-tables"

    test_helper.initialize_db()
    subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/mysql"), "-uroot", f"-S{test_helper.socket_path}", "-e", "INSTALL COMPONENT 'file://component_mysqlbackup';"],
        check=True,
    )
    test_helper.run_load(tool_options)
    test_helper.take_backup()
    test_helper.check_tables()


# Crash tests: (storage_engine, page_tracking)
CRASH_TEST_PARAMS = [
    ("innodb", False),
    ("innodb", True),
    ("rocksdb", False),
    ("rocksdb", True),
]

# One test per vault_type in KMIP_CONFIGS (used by test_kmip_component_backup and test_crash_backup_encrypted_kmip)
VAULT_TYPES = list(KMIP_CONFIGS.keys())

@pytest.mark.parametrize("storage_engine,page_tracking", CRASH_TEST_PARAMS, ids=["innodb-no_pt", "innodb-pt", "rocksdb-no_pt", "rocksdb-pt"])
def test_crash_backup(test_helper, storage_engine, page_tracking):
    """Crash test: storage engine with page-tracking on/off."""
    test_helper.run_crash_tests_pstress(storage_engine=storage_engine, page_tracking=page_tracking)


@pytest.mark.parametrize("page_tracking", [False, True], ids=["no_pt", "pt"])
def test_crash_backup_encrypted_keyring_file(test_helper, page_tracking):
    """Crash test with keyring_file encryption, page-tracking on/off (mirrors run_crash_tests_pstress_encrypted)."""
    test_helper.run_crash_tests_pstress_encrypted(page_tracking=page_tracking, vault_type=None)


@pytest.mark.parametrize("vault_type,page_tracking", [(v, pt) for v in VAULT_TYPES for pt in [False, True]], ids=[f"{v}-no_pt" if not pt else f"{v}-pt" for v in VAULT_TYPES for pt in [False, True]])
def test_crash_backup_encrypted_kmip(test_helper, vault_type, page_tracking):
    """Crash test with keyring_kmip for each vault type, page-tracking on/off."""
    test_helper.run_crash_tests_pstress_encrypted(page_tracking=page_tracking, vault_type=vault_type)


@pytest.mark.parametrize("vault_type", VAULT_TYPES)
def test_kmip_component_backup(test_helper, vault_type):
    """Test backup with keyring_kmip component for vault type."""
    test_helper.run_kmip_component_backup(vault_type)


@pytest.mark.parametrize("page_tracking", [False, True], ids=["no_pt", "pt"])
def test_kms_component_backup(test_helper, page_tracking):
    """Test backup with keyring_kms component, page-tracking on/off. Requires KMS_KEYID, KMS_SECRET_KEY, KMS_AUTH_KEY, KMS_REGION."""
    test_helper.run_kms_component_backup(page_tracking=page_tracking)


if __name__ == "__main__":
    # Allow running as a script for easier debugging
    import argparse

    parser = argparse.ArgumentParser(description="PXB Incremental Backup Load Tests")
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=["Normal_and_Encryption_tests", "Kmip_Encryption_tests", "Kms_Encryption_tests", "Rocksdb_tests", "Page_Tracking_tests", "Crash_tests"],
        help="Test suites to run",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if not args.test_suites:
        print("This script tests backup with a load tool as pquery/pstress/sysbench")
        print("Assumption: PS and PXB are already installed as tarballs")
        print("Usage: ")
        print("1. Compile pquery/pstress with mysql")
        print("2. Set environment variables (or use defaults):")
        print("   export TEST_BASE_DIR=$HOME/inc_backup_load_tests")
        print("   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.0.35-34-Linux-x86_64.glibc2.35/bin")
        print("   export MYSQLDIR=$HOME/Percona-Server-8.0.44-35-Linux.x86_64.glibc2.35")
        print("   export QASCRIPTS=$HOME/server-qa")
        print("   export LOAD_TOOL=pstress")
        print("   export LOAD_TOOL_DIR=$HOME/lab/pstress/src")
        print("   (If not set, defaults will be used from the script)")
        print("3. Run the script as: pytest inc_backup_load_tests.py -k <test_name> -s -v")
        print("   Or: python inc_backup_load_tests.py <Test Suites>")
        print("   Test Suites: ")
        print("   Normal_and_Encryption_tests")
        print("   Kmip_Encryption_tests")
        print("   Kms_Encryption_tests")
        print("   Rocksdb_tests")
        print("   Page_Tracking_tests")
        print(" ")
        print("4. Logs are available at:", TEST_BASE_DIR, "(test-specific directories)")
        sys.exit(1)

    # Run pytest with selected tests
    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    # Map test suites to test functions
    test_mapping = {
        "Normal_and_Encryption_tests": [
            "test_normal_backup",
            "test_keyring_plugin_backup",
            "test_keyring_component_backup",
            "test_memory_estimation_backup",
            "test_crash_backup[innodb-no_pt]",
            "test_crash_backup_encrypted_keyring_file",
        ],
        "Kmip_Encryption_tests": ["test_kmip_component_backup"],
        "Kms_Encryption_tests": ["test_kms_component_backup"],
        "Rocksdb_tests": ["test_rocksdb_backup", "test_crash_backup[rocksdb-no_pt or rocksdb-pt]"],
        "Page_Tracking_tests": [
            "test_page_tracking_backup",
            "test_crash_backup[innodb-pt or rocksdb-pt]",
            "test_crash_backup_encrypted_keyring_file[pt]",
        ],
        "Crash_tests": [
            "test_crash_backup",
            "test_crash_backup_encrypted_keyring_file",
            "test_crash_backup_encrypted_kmip",
        ],
    }

    selected_tests = []
    for suite in args.test_suites:
        if suite in test_mapping:
            selected_tests.extend(test_mapping[suite])

    if selected_tests:
        # Build a single -k expression
        k_expr = " or ".join(selected_tests)
        pytest_args.extend(["-k", k_expr])

    pytest.main(pytest_args)
