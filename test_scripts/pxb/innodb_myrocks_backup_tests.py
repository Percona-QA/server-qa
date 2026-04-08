#!/usr/bin/env python3
"""
This script tests backup for innodb and myrocks tables.
Rewrite of innodb_myrocks_backup_tests.sh.
Assumption: PS and PXB are already installed as tarballs.
"""

import os
import sys
import subprocess
import time
import pytest

from test_helper import BackupTestHelper, TEST_BASE_DIR, KMIP_CONFIGS


@pytest.fixture(scope="function")
def test_helper(request):
    """Create a test helper instance for each test."""
    test_name = request.node.name if hasattr(request, "node") else None
    helper = BackupTestHelper(test_name=test_name)
    helper.version, helper.version_normalized = helper.get_mysql_version()
    yield helper
    if os.environ.get("DISABLE_CLEANUP") != "1":
        helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir(test_helper):
    """Ensure log directory exists."""
    if not os.path.exists(test_helper.logdir):
        os.makedirs(test_helper.logdir)


def _default_mysqld_options():
    return "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"


def _init_for_ddl(test_helper):
    """Common initialization for DDL tests."""
    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"--core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""
    rocksdb_enabled = test_helper.rocksdb == "enabled"
    test_helper.initialize_db(rocksdb=rocksdb_enabled)

    if rocksdb_enabled:
        test_helper.run_load("", database="test", time_sec=20)
        test_helper.run_load("", database="test_rocksdb", engine="ROCKSDB", time_sec=20)
    else:
        test_helper.run_load("", time_sec=20)

    databases = ["test", "test_rocksdb"] if rocksdb_enabled else ["test"]
    return databases


# ============================================================================
# Various DDL tests
# ============================================================================

def test_inc_backup(test_helper):
    """Incremental Backup and Restore."""
    databases = _init_for_ddl(test_helper)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_index(test_helper):
    """Backup and Restore during add and drop index."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_index)
    if test_helper.version_normalized < 80000 and test_helper.server_type == "MS":
        test_helper.backup_params = "--lock-ddl-per-table"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_rename_index(test_helper):
    """Backup and Restore during rename index."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_rename_index)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_full_text_index(test_helper):
    """Backup and Restore during add and drop full text index."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_full_text_index)
    if test_helper.version_normalized < 80000 and test_helper.server_type == "MS":
        test_helper.backup_params = "--lock-ddl-per-table"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_change_index_type(test_helper):
    """Backup and Restore during index type change."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_change_index_type)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_spatial_data_index(test_helper):
    """Backup and Restore during add and drop spatial index."""
    if test_helper.version_normalized < 80000:
        pytest.skip("Spatial index tests not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper._run_sql("CREATE TABLE IF NOT EXISTS test.geom (g GEOMETRY NOT NULL SRID 0);")
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_spatial_index)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_tablespace(test_helper):
    """Backup and Restore during add and drop tablespace."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_tablespace)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_change_compression(test_helper):
    """Backup and Restore during change in compression."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_change_compression)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_change_row_format(test_helper):
    """Backup and Restore during change in row format."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_change_row_format)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_copy_data_across_engine(test_helper):
    """Backup and Restore after cross engine table copy."""
    if test_helper.rocksdb != "enabled":
        pytest.skip("RocksDB disabled")
    databases = _init_for_ddl(test_helper)
    innodb_cksum = test_helper.run_mysql_query(
        "SELECT CHECKSUM TABLE test.sbtest1;", capture=True
    )
    test_helper._run_sql("CREATE TABLE test_rocksdb.sbtestcopy LIKE test_rocksdb.sbtest1;")
    test_helper._run_sql("INSERT INTO test_rocksdb.sbtestcopy SELECT * FROM test.sbtest1;")
    test_helper.take_backup(single_incremental=True, databases=databases)
    myrocks_cksum = test_helper.run_mysql_query(
        "SELECT CHECKSUM TABLE test_rocksdb.sbtestcopy;", capture=True
    )
    innodb_val = innodb_cksum.strip().split()[-1] if innodb_cksum else ""
    myrocks_val = myrocks_cksum.strip().split()[-1] if myrocks_cksum else ""
    if innodb_val != myrocks_val:
        print(f"ERR: Checksum mismatch after cross-engine copy. "
              f"InnoDB test.sbtest1: {innodb_val}, "
              f"MyRocks test_rocksdb.sbtestcopy: {myrocks_val}")
    else:
        print(f"Checksum match after cross-engine copy: {myrocks_val}")


def test_add_data_across_engine(test_helper):
    """Backup and Restore when data is added in both engines simultaneously."""
    if test_helper.rocksdb != "enabled":
        pytest.skip("RocksDB disabled")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_data_transaction)
    test_helper.take_backup(single_incremental=True, databases=databases)

    innodb_count = test_helper.run_mysql_query(
        "SELECT count(*) FROM test.innodb_t;", capture=True
    )
    myrocks_count = test_helper.run_mysql_query(
        "SELECT count(*) FROM test.myrocks_t;", capture=True
    )
    innodb_val = innodb_count.strip() if innodb_count else "0"
    myrocks_val = myrocks_count.strip() if myrocks_count else "0"
    if innodb_val != myrocks_val:
        print(f"ERR: Row count mismatch. innodb_t: {innodb_val}, myrocks_t: {myrocks_val}")
    else:
        print(f"Row count of both tables innodb_t and myrocks_t is same after restore: Pass")

    innodb_cksum = test_helper.run_mysql_query(
        "CHECKSUM TABLE test.innodb_t;", capture=True
    )
    myrocks_cksum = test_helper.run_mysql_query(
        "CHECKSUM TABLE test.myrocks_t;", capture=True
    )
    innodb_ckval = innodb_cksum.strip().split()[-1] if innodb_cksum else ""
    myrocks_ckval = myrocks_cksum.strip().split()[-1] if myrocks_cksum else ""
    if innodb_ckval != myrocks_ckval:
        print(f"ERR: Checksum mismatch. innodb_t: {innodb_ckval}, myrocks_t: {myrocks_ckval}")
    else:
        print(f"Checksum of both tables innodb_t and myrocks_t is same after restore: Pass")


def test_update_truncate_table(test_helper):
    """Backup and Restore during update and truncate of a table."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_update_truncate_table)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_create_drop_database(test_helper):
    """Backup and Restore during create and drop of a database."""
    if test_helper.version_normalized < 80000:
        pytest.skip("Create/drop database during backup not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_create_drop_database)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_partitioned_tables(test_helper):
    """Backup and Restore during creation of partitioned tables."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_partitioned_tables)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_compressed_column(test_helper):
    """Backup and Restore during column compression."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_compressed_column)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_compression_dictionary(test_helper):
    """Backup and Restore during column compression using compression dictionary."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_compression_dictionary)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_invisible_column(test_helper):
    """Backup and Restore during add and drop of an invisible column."""
    if test_helper.version_normalized < 80000:
        pytest.skip("Invisible columns not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_invisible_column)
    test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_blob_column(test_helper):
    """Backup and Restore during add and drop of a blob column."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_blob_column)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_column_instant(test_helper):
    """Backup and Restore during column add and drop using instant algorithm."""
    if test_helper.version_normalized < 80000:
        pytest.skip("INSTANT algorithm not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_column_instant)
    time.sleep(2)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_column_algorithms(test_helper):
    """Backup and Restore during column add and drop using different algorithms."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_column_algorithms)
    time.sleep(2)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_run_all_statements(test_helper):
    """Backup and Restore during various tests running simultaneously."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_index)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_tablespace)
    test_helper.run_ddl_in_background(test_helper.ddl_change_compression)
    test_helper.run_ddl_in_background(test_helper.ddl_change_row_format)
    test_helper.run_ddl_in_background(test_helper.ddl_update_truncate_table)
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


# ============================================================================
# File encrypt/compress/stream tests
# ============================================================================

def test_streaming_backup(test_helper):
    """Incremental Backup and Restore with streaming."""
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.backup_params = f"--core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    if test_helper.version_normalized < 80000:
        print("Test: Incremental Backup and Restore with streaming format as tar")
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        test_helper.run_load("", time_sec=20)
        test_helper.take_backup(backup_type="tar", single_incremental=True)


def test_compress_stream_backup(test_helper):
    """Incremental Backup and Restore with lz4/zstd compression and streaming."""
    if test_helper.version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""

    for compress in ["lz4", "zstd"]:
        print(f"Testing {compress} compression with streaming")
        test_helper.backup_params = f"--compress={compress} --compress-threads=10 --core-file --lock-ddl={test_helper.lock_ddl}"
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        test_helper.run_load("", time_sec=20)
        test_helper.take_backup(backup_type="stream", single_incremental=True)


def test_encrypt_compress_stream_backup(test_helper):
    """Incremental Backup and Restore with encryption, compression, and streaming."""
    if test_helper.version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""

    for compress in ["lz4", "zstd"]:
        print(f"Testing {compress} compression with encryption and streaming")
        test_helper.backup_params = (
            f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
            f"--encrypt-chunk-size=128K --compress={compress} --compress-threads=10 "
            f"--core-file --lock-ddl={test_helper.lock_ddl}"
        )
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        test_helper.run_load("", time_sec=20)
        test_helper.take_backup(backup_type="stream", single_incremental=True)


def test_compress_backup(test_helper):
    """Incremental Backup and Restore with compression (no streaming)."""
    if test_helper.version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""

    compress_configs = [
        "--compress=lz4",
        "--compress=lz4 --compress-threads=10 --parallel=10",
        "--compress=lz4 --compress-chunk-size=4096K --compress-threads=100 --parallel=100",
        "--compress=zstd",
        "--compress=zstd --compress-threads=10 --parallel=10",
        "--compress=zstd --compress-chunk-size=4096K --compress-threads=100 --parallel=100",
        "--compress=zstd --compress-chunk-size=4096K --compress-threads=100 --parallel=100 --compress-zstd-level=19",
    ]
    for cfg in compress_configs:
        print(f"Testing compression: {cfg}")
        test_helper.backup_params = f"{cfg} --core-file --lock-ddl={test_helper.lock_ddl}"
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        test_helper.run_load("", time_sec=20)
        test_helper.take_backup(single_incremental=True)


# ============================================================================
# Encryption tests (8.0+)
# ============================================================================

@pytest.mark.parametrize("encrypt_type", [
    "keyring_file_plugin",
    "keyring_vault_plugin",
    "keyring_vault_component",
    "keyring_file_component",
    "keyring_kmip_component",
    "keyring_kms_component",
])
def test_encryption_8_0(test_helper, encrypt_type):
    """Encryption test suite for PXB 8.0+ / PS 8.0+."""
    if test_helper.version_normalized < 80000:
        pytest.skip("Encryption 8.0 tests require 8.0+")

    if "plugin" in encrypt_type and test_helper.version_normalized >= 80400:
        pytest.skip(f"Keyring plugins not supported in 8.4+ (detected {test_helper.version})")

    if encrypt_type == "keyring_vault_plugin":
        if test_helper.server_type == "MS":
            pytest.skip("MS 8.0 does not support keyring_vault_plugin")
        if test_helper.version_normalized >= 80100:
            pytest.skip("keyring_vault_plugin not supported in 8.1+")

    if encrypt_type == "keyring_vault_component":
        if test_helper.version_normalized < 80100:
            pytest.skip("keyring_vault_component not supported before 8.1")

    if encrypt_type == "keyring_kmip_component":
        if test_helper.server_type == "MS":
            pytest.skip("MS does not support keyring_kmip")

    if encrypt_type == "keyring_kms_component":
        if test_helper.server_type == "MS":
            pytest.skip("MS does not support keyring_kms")
        if not (test_helper.kms_id and test_helper.kms_auth_key and test_helper.kms_secret_key):
            pytest.skip("KMS tests require KMS_KEYID, KMS_AUTH_KEY, KMS_SECRET_KEY")

    try:
        _run_encryption_8_0_tests(test_helper, encrypt_type)
    finally:
        test_helper.cleanup_keyring_configs()


def _setup_encrypt_options(test_helper, encrypt_type):
    """Set up encryption options based on encrypt_type. Returns (pxb_encrypt_options, pxb_component_config, server_options)."""
    if test_helper.server_type == "MS":
        server_options = (
            "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
            "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
            "--master_verify_checksum=ON --binlog_checksum=CRC32 --binlog-rotate-encryption-master-key-at-startup "
            "--table-encryption-privilege-check=ON --max-connections=5000"
        )
    else:
        server_options = (
            "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
            "--innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON "
            "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
            "--master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files "
            "--binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON --max-connections=5000"
        )

    pxb_encrypt_options = ""
    pxb_component_config = ""

    if encrypt_type == "keyring_file_plugin":
        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring"
        else:
            pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring --xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"

    elif encrypt_type == "keyring_vault_plugin":
        vault_config = test_helper.start_vault_server()
        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_vault_config={vault_config['cnf_file']}"
        else:
            pxb_encrypt_options = f"--keyring_vault_config={vault_config['cnf_file']} --xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        server_options = (
            f"--early-plugin-load=keyring_vault=keyring_vault.so --keyring_vault_config={vault_config['cnf_file']} "
            + server_options.replace("--binlog-rotate-encryption-master-key-at-startup ", "")
        )

    elif encrypt_type == "keyring_vault_component":
        vault_config = test_helper.start_vault_server()
        test_helper.create_keyring_manifest("component_keyring_vault")
        config_file = test_helper.create_keyring_config("keyring_vault_component", vault_config=vault_config)
        if test_helper.install_type == "package":
            pxb_encrypt_options = ""
        else:
            pxb_encrypt_options = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        pxb_component_config = f"--component-keyring-config={config_file}"

    elif encrypt_type == "keyring_file_component":
        test_helper.create_keyring_manifest("component_keyring_file")
        config_file = test_helper.create_keyring_config("keyring_file_component",
                                                         keyring_path=os.path.join(test_helper.mysqldir, "lib/plugin/component_keyring_file"))
        if test_helper.install_type == "package":
            pxb_encrypt_options = ""
        else:
            pxb_encrypt_options = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        pxb_component_config = f"--component-keyring-config={config_file}"

    elif encrypt_type == "keyring_kmip_component":
        from kmip_helper import KMIPHelper
        if not test_helper.kmip_helper:
            test_helper.kmip_helper = KMIPHelper(KMIP_CONFIGS, cert_base_dir=TEST_BASE_DIR)
        for vault_type in KMIP_CONFIGS:
            if test_helper.kmip_helper.start_kmip_server(vault_type):
                break
        else:
            pytest.fail("Failed to start any KMIP server")
        test_helper.create_keyring_manifest("component_keyring_kmip")
        cert_dir = test_helper.kmip_helper.kmip_config["cert_dir"]
        config_file = test_helper.create_keyring_config("keyring_kmip_component", cert_dir=cert_dir)
        if test_helper.install_type == "package":
            pxb_encrypt_options = ""
        else:
            pxb_encrypt_options = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        pxb_component_config = f"--component-keyring-config={config_file}"

    elif encrypt_type == "keyring_kms_component":
        test_helper.create_keyring_manifest("component_keyring_kms")
        config_file = test_helper.create_keyring_config("keyring_kms_component",
                                                         keyring_path=os.path.join(test_helper.mysqldir, "keyring_kms"))
        if test_helper.install_type == "package":
            pxb_encrypt_options = ""
        else:
            pxb_encrypt_options = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        pxb_component_config = f"--component-keyring-config={config_file}"

    return pxb_encrypt_options, pxb_component_config, server_options


def _run_encryption_8_0_tests(test_helper, encrypt_type):
    """Run the full suite of encryption sub-tests for a given encrypt_type."""
    pxb_opts, pxb_comp, server_opts = _setup_encrypt_options(test_helper, encrypt_type)
    test_helper.rocksdb = "disabled"  # RocksDB tables cannot be created when encryption is enabled

    # Sub-test 1: Basic encryption
    print(f"Test: Basic {encrypt_type} encryption")
    if "plugin" in encrypt_type:
        if encrypt_type == "keyring_file_plugin":
            init_opts = f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring --default-table-encryption=ON"
        else:
            init_opts = f"{server_opts.split('--innodb')[0]} --default-table-encryption=ON"
        test_helper.mysqld_options = init_opts
    else:
        test_helper.mysqld_options = "--default-table-encryption=ON"
    test_helper.backup_params = f"{pxb_opts} --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file" if pxb_comp else f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.initialize_db()
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True)

    # Sub-test 2: All encryption options enabled
    print(f"Test: All {encrypt_type} encryption options enabled")
    if "plugin" in encrypt_type:
        test_helper.mysqld_options = f"{server_opts} --binlog-encryption"
    else:
        test_helper.mysqld_options = f"{server_opts} --binlog-encryption"
    test_helper.backup_params = f"{pxb_opts} --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file" if pxb_comp else f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.initialize_db()
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True)

    # Sub-test 3: transition-key
    print(f"Test: {encrypt_type} with transition-key")
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --core-file --lock-ddl={test_helper.lock_ddl}"
    if "plugin" in encrypt_type:
        if test_helper.install_type == "package":
            test_helper.prepare_params = f"--transition-key={test_helper.encrypt_key} --core-file"
        else:
            test_helper.prepare_params = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin --transition-key={test_helper.encrypt_key} --core-file"
        plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key --early-plugin-load={plugin_name} --core-file"
    elif pxb_comp:
        test_helper.prepare_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} {pxb_comp} --core-file"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key {pxb_comp} --core-file"
    else:
        test_helper.prepare_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --core-file"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key --core-file"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Sub-test 4: generate-transition-key
    print(f"Test: {encrypt_type} with generate-transition-key")
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --generate-transition-key --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file" if pxb_comp else f"{pxb_opts} --core-file"
    if pxb_comp:
        test_helper.restore_params = f"{pxb_opts} {pxb_comp} --generate-new-master-key --core-file"
    else:
        if "plugin" in encrypt_type:
            plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
            test_helper.restore_params = f"{pxb_opts} --generate-new-master-key --early-plugin-load={plugin_name} --core-file"
        else:
            test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Sub-test 5: lz4 compression with streaming
    print(f"Test: {encrypt_type} with lz4 compression and streaming")
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress=lz4 --compress-threads=10 --core-file --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file" if pxb_comp else f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # Sub-test 6: zstd compression with streaming
    print(f"Test: {encrypt_type} with zstd compression and streaming")
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress=zstd --compress-threads=10 --core-file --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file" if pxb_comp else f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # Sub-test 7+: DDL tests with encryption
    print(f"Test: DDL sub-tests with {encrypt_type}")
    if pxb_comp:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl --core-file"
        test_helper.prepare_params = f"{pxb_opts} {pxb_comp} --core-file"
    else:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl --core-file"
        test_helper.prepare_params = f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.mysqld_options = server_opts

    ddl_funcs = [
        test_helper.ddl_add_drop_index,
        test_helper.ddl_add_drop_tablespace,
        test_helper.ddl_change_compression,
        test_helper.ddl_change_row_format,
        test_helper.ddl_update_truncate_table,
        test_helper.ddl_create_drop_database,
        test_helper.ddl_rename_index,
        test_helper.ddl_add_drop_full_text_index,
        test_helper.ddl_change_index_type,
        test_helper.ddl_add_drop_spatial_index,
        test_helper.ddl_create_delete_encrypted_table,
        test_helper.ddl_partitioned_tables,
        test_helper.ddl_compressed_column,
        test_helper.ddl_compression_dictionary,
        test_helper.ddl_change_encryption,
    ]

    for ddl_func in ddl_funcs:
        print(f"  Sub-test: {ddl_func.__name__}")
        test_helper.run_ddl_in_background(ddl_func)
        test_helper.take_backup(single_incremental=True)


# ============================================================================
# Encryption tests (2.4 / 5.7)
# ============================================================================

@pytest.mark.parametrize("encrypt_type", ["keyring_file_plugin", "keyring_vault_plugin"])
def test_encryption_2_4(test_helper, encrypt_type):
    """Encryption test suite for PXB 2.4 / PS 5.7."""
    if test_helper.version_normalized >= 80000:
        pytest.skip("2.4 encryption tests are for 5.7 only")

    if encrypt_type == "keyring_vault_plugin" and test_helper.server_type == "MS":
        pytest.skip("MS 5.7 does not support keyring_vault")

    try:
        _run_encryption_2_4_tests(test_helper, encrypt_type)
    finally:
        test_helper.cleanup_keyring_configs()


def _run_encryption_2_4_tests(test_helper, encrypt_type):
    """Run the encryption sub-tests for PXB 2.4."""
    test_helper.rocksdb = "disabled"

    if encrypt_type == "keyring_file_plugin":
        if test_helper.server_type == "MS":
            server_opts = f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"
        else:
            server_opts = f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring --innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt --innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-binlog"
        if test_helper.install_type == "package":
            pxb_opts = f"--keyring_file_data={test_helper.mysqldir}/keyring"
        else:
            pxb_opts = f"--keyring_file_data={test_helper.mysqldir}/keyring --xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
    else:
        vault_config = test_helper.start_vault_server()
        if test_helper.install_type == "package":
            pxb_opts = f"--keyring_vault_config={vault_config['cnf_file']}"
        else:
            pxb_opts = f"--keyring_vault_config={vault_config['cnf_file']} --xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        server_opts = f"--early-plugin-load=keyring_vault=keyring_vault.so --keyring_vault_config={vault_config['cnf_file']} --innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt --innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"

    # Basic encrypted backup
    test_helper.mysqld_options = server_opts
    test_helper.backup_params = f"{pxb_opts} --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.initialize_db()
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True)

    # Transition-key test
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --core-file --lock-ddl={test_helper.lock_ddl}"
    if test_helper.install_type == "package":
        test_helper.prepare_params = f"--transition-key={test_helper.encrypt_key} --core-file"
    else:
        test_helper.prepare_params = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin --transition-key={test_helper.encrypt_key} --core-file"
    plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
    test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key --early-plugin-load={plugin_name} --core-file"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Streaming with compression
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress --compress-threads=10 --core-file --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # DDL sub-tests
    if test_helper.server_type == "MS":
        test_helper.backup_params = f"{pxb_opts} --lock-ddl-per-table --core-file"
    else:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl --core-file"
        if encrypt_type == "keyring_file_plugin":
            no_binlog_opts = f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring --innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt --innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"
            test_helper.mysqld_options = no_binlog_opts
            test_helper.initialize_db()
    test_helper.prepare_params = f"{pxb_opts} --core-file"
    test_helper.restore_params = f"{pxb_opts} --core-file"

    ddl_funcs = [
        test_helper.ddl_add_drop_index,
        test_helper.ddl_add_drop_tablespace,
        test_helper.ddl_change_compression,
        test_helper.ddl_change_row_format,
        test_helper.ddl_update_truncate_table,
        test_helper.ddl_rename_index,
        test_helper.ddl_add_drop_full_text_index,
        test_helper.ddl_change_index_type,
        test_helper.ddl_create_delete_encrypted_table,
        test_helper.ddl_partitioned_tables,
        test_helper.ddl_compressed_column,
        test_helper.ddl_compression_dictionary,
        test_helper.ddl_change_encryption,
    ]
    for ddl_func in ddl_funcs:
        print(f"  Sub-test: {ddl_func.__name__}")
        test_helper.run_ddl_in_background(ddl_func)
        test_helper.take_backup(single_incremental=True)


# ============================================================================
# Cloud backup tests
# ============================================================================

def test_cloud_inc_backup(test_helper):
    """Cloud incremental backup tests."""
    cloud_params = f"--defaults-file={test_helper.cloud_config} --verbose"

    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"--parallel=10 --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    test_helper.run_load("", time_sec=20)

    print("Test: Cloud backup")
    test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)

    print("Test: Cloud backup with encryption")
    test_helper.backup_params = (
        f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --core-file --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)

    if test_helper.version_normalized >= 80000:
        for compress in ["lz4", "zstd"]:
            print(f"Test: Cloud backup with {compress} compression and encryption")
            test_helper.backup_params = (
                f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
                f"--encrypt-chunk-size=128K --compress={compress} --compress-threads=10 "
                f"--core-file --lock-ddl={test_helper.lock_ddl}"
            )
            test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)


# ============================================================================
# InnoDB params and redo archive tests
# ============================================================================

def test_inc_backup_innodb_params(test_helper):
    """Backup and Restore with different InnoDB parameter values."""
    if test_helper.version_normalized < 80000:
        pytest.skip("InnoDB params tests require 8.0+")

    test_helper.backup_params = f"--core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.restore_params = ""

    configs = [
        {"mysqld": "--innodb-redo-log-capacity=209715200", "backup_extra": "--innodb-log-file-size=209715200",
         "prepare_extra": "--innodb-log-file-size=209715200 --core-file"},
        {"mysqld": "--innodb-redo-log-capacity=2147483648", "backup_extra": "--innodb-log-file-size=2147483648",
         "prepare_extra": "--innodb-log-file-size=2147483648 --core-file"},
        {"mysqld": "--innodb-redo-log-capacity=8388608 --innodb-buffer-pool-size=2G",
         "backup_extra": "--innodb-log-file-size=8388608 --innodb-buffer-pool-size=2G",
         "prepare_extra": "--innodb-log-file-size=8388608 --innodb-buffer-pool-size=2G --core-file"},
        {"mysqld": "--skip-log-bin", "backup_extra": "", "prepare_extra": "--core-file"},
    ]

    for cfg in configs:
        print(f"Test: InnoDB params: {cfg['mysqld']}")
        test_helper.mysqld_options = cfg["mysqld"]
        test_helper.backup_params = f"{cfg['backup_extra']} --core-file --lock-ddl={test_helper.lock_ddl}" if cfg["backup_extra"] else f"--core-file --lock-ddl={test_helper.lock_ddl}"
        test_helper.prepare_params = cfg["prepare_extra"]
        test_helper.initialize_db()
        test_helper.run_load("", time_sec=20)
        test_helper.take_backup(single_incremental=True)


def test_inc_backup_archive_log(test_helper):
    """Backup and Restore with redo archive log."""
    if test_helper.version_normalized < 80000:
        pytest.skip("Redo archive log tests require 8.0+")

    archive_dir = os.path.join(test_helper.mysqldir, "archive")
    os.makedirs(archive_dir, mode=0o744, exist_ok=True)

    test_helper.backup_params = f"--core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""

    test_helper.mysqld_options = (
        f"--innodb-extend-and-initialize=OFF --innodb-log-writer-threads=OFF "
        f"--innodb-redo-log-archive-dirs=archive:{archive_dir}"
    )
    test_helper.initialize_db()
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True)

    test_helper.mysqld_options = (
        f"--innodb-redo-log-capacity=536870912 --binlog-transaction-compression=ON "
        f"--binlog-transaction-compression-level-zstd=22 --innodb-extend-and-initialize=OFF "
        f"--innodb-log-writer-threads=OFF --innodb-redo-log-archive-dirs=archive:{archive_dir}"
    )
    test_helper.prepare_params = "--innodb-log-file-size=536870912 --core-file"
    test_helper.initialize_db()
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True)


# ============================================================================
# SSL tests
# ============================================================================

def test_ssl_backup(test_helper):
    """Backup and Restore with SSL options."""
    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"--core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = "--core-file"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    databases = ["test", "test_rocksdb"] if test_helper.rocksdb == "enabled" else ["test"]

    datadir = test_helper.datadir
    ssl_opts = f"--ssl-ca={datadir}/ca.pem --ssl-cert={datadir}/server-cert.pem --ssl-key={datadir}/server-key.pem"

    # Restart with SSL
    subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/mysqladmin"), "-uroot", f"-S{test_helper.socket_path}", "shutdown"],
        check=False,
    )
    test_helper.mysqld_options += f" {ssl_opts}"
    test_helper.start_server()

    # Create backup user with SSL
    test_helper._run_sql("CREATE USER IF NOT EXISTS 'backup'@'localhost' REQUIRE SSL;")
    test_helper._run_sql("GRANT ALL ON *.* TO 'backup'@'localhost';")
    test_helper.backup_user = "backup"

    print("Test: Backup with SSL certificates and keys")
    test_helper.backup_params = f"{ssl_opts} --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True, databases=databases)

    print("Test: Backup with --ssl-mode")
    port_result = subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/mysql"), "-uroot", f"-S{test_helper.socket_path}", "-Bse", "SELECT @@port;"],
        capture_output=True, text=True, check=False,
    )
    mysql_port = port_result.stdout.strip() if port_result.returncode == 0 else "21000"
    test_helper.backup_params = f"{ssl_opts} --ssl-mode=REQUIRED --host=127.0.0.1 -P {mysql_port} --core-file --lock-ddl={test_helper.lock_ddl}"
    test_helper.run_load("", time_sec=20)
    test_helper.take_backup(single_incremental=True, databases=databases)

    test_helper.backup_user = "root"


# ============================================================================
# __main__ block with argparse and suite-to-test mapping
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PXB InnoDB/MyRocks Backup Tests")
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=[
            "Various_ddl_tests",
            "File_encrypt_compress_stream_tests",
            "Encryption_PXB2_4_PS5_7_tests",
            "Encryption_PXB2_4_MS5_7_tests",
            "Encryption_PXB8_0_PS8_0_tests",
            "Encryption_PXB9_0_PS9_0_tests",
            "Encryption_PXB8_0_PS8_0_KMIP_tests",
            "Encryption_PXB8_0_PS8_0_KMS_tests",
            "Encryption_PXB8_0_MS8_0_tests",
            "Encryption_PXB9_0_MS9_0_tests",
            "Cloud_backup_tests",
            "Innodb_params_redo_archive_tests",
            "SSL_tests",
        ],
        help="Test suites to run",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if not args.test_suites:
        print("This script tests backup for innodb and myrocks tables")
        print("Assumption: PS and PXB are already installed as tarballs")
        print("Usage: ")
        print("1. Set environment variables (or use defaults):")
        print("   export TEST_BASE_DIR=$HOME/inc_backup_load_tests")
        print("   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.0/bin")
        print("   export MYSQLDIR=$HOME/Percona-Server-8.0")
        print("   export QASCRIPTS=$HOME/server-qa")
        print("   export LOAD_TOOL=sysbench")
        print("   export ROCKSDB=enabled  # or disabled")
        print("   export CLOUD_CONFIG=$HOME/aws.cnf")
        print("   export INSTALL_TYPE=tarball  # or package")
        print("2. Run the script as: pytest innodb_myrocks_backup_tests.py -k <test_name> -s -v")
        print("   Or: python innodb_myrocks_backup_tests.py <Test Suites>")
        print("   Test Suites:")
        print("   Various_ddl_tests")
        print("   File_encrypt_compress_stream_tests")
        print("   Encryption_PXB2_4_PS5_7_tests")
        print("   Encryption_PXB2_4_MS5_7_tests")
        print("   Encryption_PXB8_0_PS8_0_tests")
        print("   Encryption_PXB9_0_PS9_0_tests")
        print("   Encryption_PXB8_0_PS8_0_KMIP_tests")
        print("   Encryption_PXB8_0_PS8_0_KMS_tests")
        print("   Encryption_PXB8_0_MS8_0_tests")
        print("   Encryption_PXB9_0_MS9_0_tests")
        print("   Cloud_backup_tests")
        print("   Innodb_params_redo_archive_tests")
        print("   SSL_tests")
        print("")
        print("3. Logs are available at:", TEST_BASE_DIR, "(test-specific directories)")
        sys.exit(1)

    test_mapping = {
        "Various_ddl_tests": [
            "test_inc_backup", "test_add_drop_index", "test_rename_index",
            "test_add_drop_full_text_index", "test_change_index_type",
            "test_spatial_data_index", "test_add_drop_tablespace",
            "test_change_compression", "test_change_row_format",
            "test_copy_data_across_engine", "test_add_data_across_engine",
            "test_update_truncate_table", "test_create_drop_database",
            "test_partitioned_tables", "test_compressed_column",
            "test_compression_dictionary", "test_invisible_column",
            "test_blob_column", "test_add_drop_column_instant",
            "test_add_drop_column_algorithms", "test_run_all_statements",
        ],
        "File_encrypt_compress_stream_tests": [
            "test_streaming_backup", "test_compress_stream_backup",
            "test_encrypt_compress_stream_backup", "test_compress_backup",
        ],
        "Encryption_PXB8_0_PS8_0_tests": [
            "test_encryption_8_0[keyring_file_plugin]",
            "test_encryption_8_0[keyring_vault_plugin]",
            "test_encryption_8_0[keyring_vault_component]",
            "test_encryption_8_0[keyring_file_component]",
        ],
        "Encryption_PXB9_0_PS9_0_tests": [
            "test_encryption_8_0[keyring_file_component]",
            "test_encryption_8_0[keyring_vault_component]",
        ],
        "Encryption_PXB8_0_PS8_0_KMIP_tests": [
            "test_encryption_8_0[keyring_kmip_component]",
        ],
        "Encryption_PXB8_0_PS8_0_KMS_tests": [
            "test_encryption_8_0[keyring_kms_component]",
        ],
        "Encryption_PXB8_0_MS8_0_tests": [
            "test_encryption_8_0[keyring_file_plugin]",
            "test_encryption_8_0[keyring_file_component]",
        ],
        "Encryption_PXB9_0_MS9_0_tests": [
            "test_encryption_8_0[keyring_file_component]",
        ],
        "Encryption_PXB2_4_PS5_7_tests": [
            "test_encryption_2_4[keyring_file_plugin]",
            "test_encryption_2_4[keyring_vault_plugin]",
        ],
        "Encryption_PXB2_4_MS5_7_tests": [
            "test_encryption_2_4[keyring_file_plugin]",
        ],
        "Cloud_backup_tests": ["test_cloud_inc_backup"],
        "Innodb_params_redo_archive_tests": [
            "test_inc_backup_innodb_params",
            "test_inc_backup_archive_log",
        ],
        "SSL_tests": ["test_ssl_backup"],
    }

    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    selected_tests = []
    for suite in args.test_suites:
        if suite in test_mapping:
            selected_tests.extend(test_mapping[suite])

    if selected_tests:
        k_expr = " or ".join(selected_tests)
        pytest_args.extend(["-k", k_expr])

    pytest.main(pytest_args)
