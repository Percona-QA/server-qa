#!/usr/bin/env python3
"""
PXB FIFO-streamed cloud backup tests.

Pytest port of xbstream_fifo_test.sh: full/incremental/compressed/
partition-table/encrypted backups streamed through xtrabackup/xbcloud/
xbstream named pipes (--fifo-streams/--fifo-dir) to a local SeaweedFS
S3-gateway container, instead of a single-process shell pipe.

Assumption: PS and PXB are already installed as tarballs, and Docker is
installed and running (for the SeaweedFS container).
"""

import os
import pytest

from test_helper import BackupTestHelper, TEST_BASE_DIR, KMIP_CONFIGS, CORE_FILE_OPT
from seaweedfs_helper import SeaweedFSHelper

try:
    from kmip_helper import KMIPHelper
except ImportError:
    KMIPHelper = None

# Local S3 credentials/bucket for the SeaweedFS container (matches
# xbstream_fifo_test.sh's hardcoded admin/password/my-bucket).
SEAWEEDFS_BUCKET = "my-bucket"
SEAWEEDFS_ACCESS_KEY = "admin"
SEAWEEDFS_SECRET_KEY = "password"
SEAWEEDFS_REGION = "us-east-1"

VAULT_TYPES = list(KMIP_CONFIGS.keys())


# Pytest fixtures
@pytest.fixture(scope="session")
def seaweedfs():
    """Start the local SeaweedFS S3-gateway container once for the whole
    test session (mirrors xbstream_fifo_test.sh, which starts the container
    once at the top of the script and leaves it running across scenarios),
    and stop it when the session ends."""
    helper = SeaweedFSHelper()
    if not helper.start():
        pytest.fail(f"Failed to start SeaweedFS: {helper.last_error}")
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.stop()


@pytest.fixture(scope="function")
def test_helper(request):
    """Create a test helper instance for each test."""
    test_name = request.node.name if hasattr(request, "node") else None
    helper = BackupTestHelper(test_name=test_name)
    helper.server_version, helper.server_version_normalized = helper.get_mysql_version()
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir(test_helper):
    """Ensure log directory exists."""
    if not os.path.exists(test_helper.logdir):
        os.makedirs(test_helper.logdir)


@pytest.fixture(scope="function")
def cloud_params(seaweedfs, test_helper):
    """Point test_helper at the local SeaweedFS container and return the
    assembled xbcloud S3 option string."""
    test_helper.s3_bucket = SEAWEEDFS_BUCKET
    test_helper.s3_access_key = SEAWEEDFS_ACCESS_KEY
    test_helper.s3_secret_key = SEAWEEDFS_SECRET_KEY
    test_helper.s3_region = SEAWEEDFS_REGION
    test_helper.s3_endpoint = f"http://localhost:{seaweedfs.host_port}"
    return test_helper.build_cloud_params()


# Module-level helpers (mirrors the _pstress_tool_options()/_run_load() style
# used by innodb_myrocks_backup_tests.py)
def _default_tool_options(test_helper) -> str:
    opts = (
        f"--tables {test_helper.num_tables} --records {test_helper.table_size} "
        f"--threads {test_helper.threads} --seconds {test_helper.seconds} "
        "--no-encryption --undo-tbs-sql 0"
    )
    if test_helper.server_type == "MS":
        opts += " --no-column-compression --no-temp-tables"
    return opts


def _pstress_tool_options(seconds: int = 120, only_partition_tables: bool = False, no_encryption: bool = True) -> str:
    opts = f"--tables 150 --records 1000 --seconds {seconds} --threads 10"
    if no_encryption:
        opts += " --no-encryption"
    if only_partition_tables:
        opts += " --only-partition-tables"
    return opts


# Test functions
def test_fifo_full_backup_and_restore(test_helper, cloud_params):
    """Full backup and restore streamed via FIFO to SeaweedFS."""
    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.initialize_db()
    test_helper.run_load(_default_tool_options(test_helper))

    test_helper.cleanup_fifo_state(cloud_params, ["full_backup"])
    full_target = test_helper.take_fifo_full_backup_and_restore(cloud_params)
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


def test_fifo_incremental_backup(test_helper, cloud_params):
    """Full + 3 incremental backups (5s apart) streamed via FIFO to SeaweedFS."""
    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.initialize_db()
    test_helper.run_load(_default_tool_options(test_helper))

    test_helper.cleanup_fifo_state(cloud_params, ["full", "inc1", "inc2", "inc3"])
    full_target = test_helper.take_fifo_incremental_backup_and_restore(cloud_params)
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


def test_fifo_compressed_backup(test_helper, cloud_params):
    """Full backup with zstd compression streamed via FIFO to SeaweedFS."""
    test_helper.backup_params = f"--compress=zstd --compress-zstd-level=19 --compress-threads=10 {CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.initialize_db()
    test_helper.run_load(_default_tool_options(test_helper))

    test_helper.cleanup_fifo_state(cloud_params, ["full_backup"])
    full_target = test_helper.take_fifo_full_backup_and_restore(cloud_params)
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


def test_fifo_partition_tables(test_helper, cloud_params):
    """Incremental backup of pstress-generated partitioned tables, streamed via FIFO to SeaweedFS."""
    if test_helper.load_tool != "pstress":
        pytest.skip("Partition-table load requires LOAD_TOOL=pstress")

    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.initialize_db()
    test_helper.run_load(_pstress_tool_options(seconds=120, only_partition_tables=True))

    test_helper.cleanup_fifo_state(cloud_params, ["full", "inc1", "inc2", "inc3"])
    full_target = test_helper.take_fifo_incremental_backup_and_restore(cloud_params)
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


def test_fifo_keyring_file_backup(test_helper, cloud_params):
    """keyring_file encrypted incremental backup streamed via FIFO to SeaweedFS.

    Known issue: as of PXB 8.4.0-7 / PS 8.4.10-10, taking an incremental
    backup of a keyring_file-encrypted tablespace can crash xtrabackup with
    an InnoDB assertion (fil0fil.cc:10943:page_id.space() != TRX_SYS_SPACE)
    while parsing the redo log. That's a product-level PXB/InnoDB bug (not
    a bug in the FIFO/SeaweedFS plumbing here) -- file/track it against PXB
    separately if hit; this test still documents/exercises the scenario.
    """
    if test_helper.load_tool != "pstress":
        pytest.skip("keyring_file encrypted-table load requires LOAD_TOOL=pstress")

    test_helper.create_keyring_manifest("component_keyring_file")
    config_file = test_helper.create_keyring_config(
        "keyring_file", keyring_path=os.path.join(test_helper.logdir, "keyring")
    )

    keyring_backup_opts = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT} {keyring_backup_opts} --component-keyring-config={config_file}"
    test_helper.restore_params = test_helper.prepare_params

    test_helper.initialize_db()
    # No --no-encryption: pstress creates some ENCRYPTION='Y' tables, matching
    # xbstream_fifo_test.sh's keyring_file scenario.
    test_helper.run_load(_pstress_tool_options(seconds=120, no_encryption=False))

    test_helper.cleanup_fifo_state(cloud_params, ["full", "inc1", "inc2", "inc3"])
    full_target = test_helper.take_fifo_incremental_backup_and_restore(
        cloud_params, keyring_backup_opts=keyring_backup_opts
    )
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


@pytest.mark.parametrize("vault_type", VAULT_TYPES)
def test_fifo_kmip_backup(test_helper, cloud_params, vault_type):
    """keyring_kmip encrypted incremental backup streamed via FIFO to SeaweedFS."""
    if test_helper.load_tool != "pstress":
        pytest.skip("keyring_kmip encrypted-table load requires LOAD_TOOL=pstress")
    if test_helper.server_version_normalized < 80000:
        pytest.skip("KMIP component is not supported in 5.7")
    if test_helper.server_type == "MS":
        pytest.skip("MS does not support keyring kmip for encryption")
    if not KMIPHelper:
        pytest.skip("KMIP helper not available (kmip_helper module)")
    if vault_type not in KMIP_CONFIGS:
        pytest.skip(f"Unknown vault_type '{vault_type}'. Available: {list(KMIP_CONFIGS.keys())}")
    if vault_type == "fortanix" and (
        not os.environ.get("FORTANIX_EMAIL", "").strip() or not os.environ.get("FORTANIX_PASSWORD", "").strip()
    ):
        pytest.skip("Fortanix KMIP requires FORTANIX_EMAIL and FORTANIX_PASSWORD environment variables")

    if not test_helper.kmip_helper:
        test_helper.kmip_helper = KMIPHelper(KMIP_CONFIGS, cert_base_dir=TEST_BASE_DIR)
    if not test_helper.kmip_helper.start_kmip_server(vault_type):
        detail = getattr(test_helper.kmip_helper, "last_error", None) or "unknown"
        pytest.fail(f"Failed to start KMIP server for vault_type={vault_type}. {detail}")

    test_helper.create_keyring_manifest("component_keyring_kmip")
    config_file = test_helper.create_keyring_config(
        "keyring_kmip", cert_dir=test_helper.kmip_helper.kmip_config["cert_dir"]
    )

    keyring_backup_opts = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT} {keyring_backup_opts} --component-keyring-config={config_file}"
    test_helper.restore_params = test_helper.prepare_params

    test_helper.initialize_db()
    test_helper.run_load(_pstress_tool_options(seconds=120, no_encryption=False))

    test_helper.cleanup_fifo_state(cloud_params, ["full", "inc1", "inc2", "inc3"])
    full_target = test_helper.take_fifo_incremental_backup_and_restore(
        cloud_params, keyring_backup_opts=keyring_backup_opts
    )
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


def test_fifo_encrypted_backup(test_helper, cloud_params):
    """Full backup encrypted with xbcrypt (--encrypt/--encrypt-key, not
    keyring-based), streamed via FIFO to SeaweedFS."""
    test_helper.backup_params = f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.initialize_db()
    test_helper.run_load(_pstress_tool_options(seconds=60))

    test_helper.cleanup_fifo_state(cloud_params, ["full_backup"])
    full_target = test_helper.take_fifo_full_backup_and_restore(cloud_params)
    test_helper.restore_datadir_from(full_target)
    test_helper.check_tables()


if __name__ == "__main__":
    # Allow running as a script for easier debugging
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="PXB FIFO-streamed cloud backup tests")
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=["Fifo_Backup_tests", "Fifo_Partition_tests", "Fifo_Encryption_tests", "Fifo_Kmip_tests"],
        help="Test suites to run",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if not args.test_suites:
        print("This script tests FIFO-streamed backups (xtrabackup/xbcloud/xbstream named pipes) against a local SeaweedFS S3 gateway")
        print("Assumption: PS and PXB are already installed as tarballs, and Docker is installed and running")
        print("Usage: ")
        print("1. Set environment variables (or use defaults):")
        print("   export TEST_BASE_DIR=$HOME/inc_backup_load_tests")
        print("   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.4.0-7-Linux-x86_64.glibc2.36-minimal/bin")
        print("   export MYSQLDIR=$HOME/Percona-Server-8.4.10-10-Linux.x86_64.glibc2.35-minimal")
        print("   export QASCRIPTS=$HOME/server-qa")
        print("   export LOAD_TOOL=pstress")
        print("   export LOAD_TOOL_DIR=$HOME/pstress/src")
        print("   export FIFO_STREAM=30")
        print("   export FIFO_DIR=/tmp/xbstream_fifo")
        print("   (If not set, defaults will be used from the script)")
        print("2. Run the script as: pytest xbstream_fifo_tests.py -k <test_name> -s -v")
        print("   Or: python xbstream_fifo_tests.py <Test Suites>")
        print("   Test Suites: ")
        print("   Fifo_Backup_tests")
        print("   Fifo_Partition_tests")
        print("   Fifo_Encryption_tests")
        print("   Fifo_Kmip_tests")
        print(" ")
        print("3. Logs are available at:", TEST_BASE_DIR, "(test-specific directories)")
        sys.exit(1)

    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    test_mapping = {
        "Fifo_Backup_tests": [
            "test_fifo_full_backup_and_restore",
            "test_fifo_incremental_backup",
            "test_fifo_compressed_backup",
        ],
        "Fifo_Partition_tests": ["test_fifo_partition_tables"],
        "Fifo_Encryption_tests": ["test_fifo_keyring_file_backup", "test_fifo_encrypted_backup"],
        "Fifo_Kmip_tests": ["test_fifo_kmip_backup"],
    }

    selected_tests = []
    for suite in args.test_suites:
        if suite in test_mapping:
            selected_tests.extend(test_mapping[suite])

    if selected_tests:
        k_expr = " or ".join(selected_tests)
        pytest_args.extend(["-k", k_expr])

    pytest.main(pytest_args)
