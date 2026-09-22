#!/usr/bin/env python3
"""
This script tests backup for innodb and myrocks tables.
Rewrite of innodb_myrocks_backup_tests.sh.
Assumption: PS and PXB are already installed as tarballs.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import pytest
from test_helper import CORE_FILE_OPT, KMIP_CONFIGS, TEST_BASE_DIR, BackupTestHelper


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


def _default_mysqld_options():
    return "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"


def _pstress_tool_options(test_helper, seconds=20, rocksdb=False):
    """Build pstress options (aligned with inc_backup_load_tests.py)."""
    tool_options = (
        f"--tables {test_helper.num_tables} --records {test_helper.table_size} "
        f"--threads {test_helper.threads} --seconds {seconds} --no-encryption --undo-tbs-sql 0"
    )
    if test_helper.server_type == "MS":
        tool_options += " --no-column-compression --no-temp-tables"
    if rocksdb:
        tool_options += " --engine=rocksdb --no-fk-tables"
    return tool_options


def _run_load(test_helper, time_sec=20):
    """Run backup load; pstress gets explicit options, sysbench keeps legacy empty options."""
    if test_helper.load_tool == "pstress":
        if test_helper.rocksdb == "enabled":
            test_helper.run_load(_pstress_tool_options(test_helper, seconds=time_sec))
            test_helper.run_load(_pstress_tool_options(test_helper, seconds=time_sec, rocksdb=True))
        else:
            test_helper.run_load(_pstress_tool_options(test_helper, seconds=time_sec))
    elif test_helper.rocksdb == "enabled":
        test_helper.run_load("", database="test", time_sec=time_sec)
        test_helper.run_load("", database="test_rocksdb", engine="ROCKSDB", time_sec=time_sec)
    else:
        test_helper.run_load("", time_sec=time_sec)


def _init_for_ddl(test_helper):
    """Common initialization for DDL tests."""
    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    rocksdb_enabled = test_helper.rocksdb == "enabled"
    test_helper.initialize_db(rocksdb=rocksdb_enabled)

    _run_load(test_helper)

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
    if test_helper.server_version_normalized < 80000 and test_helper.server_type == "MS":
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
    if test_helper.server_version_normalized < 80000 and test_helper.server_type == "MS":
        test_helper.backup_params = "--lock-ddl-per-table"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_change_index_type(test_helper):
    """Backup and Restore during index type change."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_change_index_type)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_spatial_data_index(test_helper):
    """Backup and Restore during add and drop spatial index."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Spatial index tests not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper._run_sql("CREATE TABLE IF NOT EXISTS test.geom (g GEOMETRY NOT NULL SRID 0);")
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_spatial_index)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_tablespace(test_helper):
    """Backup and Restore during add and drop tablespace."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_tablespace)
    if test_helper.server_version_normalized < 80000:
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
    if test_helper.server_version_normalized < 80000:
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

    # Wait for the background sysbench load to finish so test.sbtest1 is
    # quiesced before we copy from it; otherwise CHECKSUM TABLE on the source
    # and the copy would observe different snapshots.
    while test_helper.is_load_running():
        time.sleep(1)

    # Build sbtestcopy with the same schema as the InnoDB source and only
    # switch the storage engine to ROCKSDB. This guarantees CHECKSUM TABLE
    # is comparable across engines (column types, indexes, charset, etc. are
    # identical; only the engine differs).
    test_helper._run_sql("DROP TABLE IF EXISTS test_rocksdb.sbtestcopy;")
    test_helper._run_sql("CREATE TABLE test_rocksdb.sbtestcopy LIKE test.sbtest1;")
    test_helper._run_sql("ALTER TABLE test_rocksdb.sbtestcopy ENGINE=ROCKSDB;")
    test_helper._run_sql("INSERT INTO test_rocksdb.sbtestcopy SELECT * FROM test.sbtest1;")

    test_helper.take_backup(single_incremental=True, databases=databases)

    # Both checksums are taken after the backup+restore cycle so they reflect
    # the same temporal snapshot (the restored datadir).
    innodb_cksum = test_helper.run_mysql_query(
        "CHECKSUM TABLE test.sbtest1;", capture=True
    )
    myrocks_cksum = test_helper.run_mysql_query(
        "CHECKSUM TABLE test_rocksdb.sbtestcopy;", capture=True
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
    if test_helper.server_version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_create_drop_database(test_helper):
    """Backup and Restore during create and drop of a database."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Create/drop database during backup not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_create_drop_database)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_partitioned_tables(test_helper):
    """Backup and Restore during creation of partitioned tables."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_partitioned_tables)
    if test_helper.server_version_normalized < 80000:
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
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Invisible columns not supported in 5.7")
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_invisible_column)
    test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_blob_column(test_helper):
    """Backup and Restore during add and drop of a blob column."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_add_drop_blob_column)
    if test_helper.server_version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.backup_params = "--lock-ddl-per-table"
        else:
            test_helper.backup_params += " --lock-ddl"
    test_helper.take_backup(single_incremental=True, databases=databases)


@pytest.mark.skip(reason="Disabled due to Bug https://jira.percona.com/browse/PS-8950")
def test_grant_tables(test_helper):
    """Backup and Restore during creation and dropping of a user."""
    databases = _init_for_ddl(test_helper)
    test_helper.run_ddl_in_background(test_helper.ddl_grant_tables)
    test_helper.take_backup(single_incremental=True, databases=databases)


def test_add_drop_column_instant(test_helper):
    """Backup and Restore during column add and drop using instant algorithm."""
    if test_helper.server_version_normalized < 80000:
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
    if test_helper.server_version_normalized < 80000:
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
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    _run_load(test_helper)
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    if test_helper.server_version_normalized < 80000:
        print("Test: Incremental Backup and Restore with streaming format as tar")
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        _run_load(test_helper)
        test_helper.take_backup(backup_type="tar", single_incremental=True)


def test_compress_stream_backup(test_helper):
    """Incremental Backup and Restore with lz4/zstd compression and streaming."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    for compress in ["lz4", "zstd"]:
        print(f"Testing {compress} compression with streaming")
        test_helper.backup_params = f"--compress={compress} --compress-threads=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        _run_load(test_helper)
        test_helper.take_backup(backup_type="stream", single_incremental=True)


def test_encrypt_compress_stream_backup(test_helper):
    """Incremental Backup and Restore with encryption, compression, and streaming."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    for compress in ["lz4", "zstd"]:
        print(f"Testing {compress} compression with encryption and streaming")
        test_helper.backup_params = (
            f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
            f"--encrypt-chunk-size=128K --compress={compress} --compress-threads=10 "
            f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
        )
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        _run_load(test_helper)
        test_helper.take_backup(backup_type="stream", single_incremental=True)


def test_compress_backup(test_helper):
    """Incremental Backup and Restore with compression (no streaming)."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("lz4/zstd compression not supported in 5.7")
    test_helper.mysqld_options = "--log-bin=binlog"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
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
        test_helper.backup_params = f"{cfg} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
        test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
        _run_load(test_helper)
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
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Encryption 8.0 tests require 8.0+")

    if "plugin" in encrypt_type and test_helper.server_version_normalized >= 80400:
        pytest.skip(f"Keyring plugins not supported in 8.4+ (detected {test_helper.server_version})")

    if encrypt_type == "keyring_vault_plugin":
        if test_helper.server_type == "MS":
            pytest.skip("MS 8.0 does not support keyring_vault_plugin")
        if test_helper.server_version_normalized >= 80100:
            pytest.skip("keyring_vault_plugin not supported in 8.1+")

    if encrypt_type == "keyring_vault_component":
        if test_helper.server_version_normalized < 80100:
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
        server_options = (
            f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring "
            + server_options
        )

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
    test_helper.backup_params = f"{pxb_opts} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}" if pxb_comp else f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.initialize_db()
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True)

    # Sub-test 2: All encryption options enabled
    print(f"Test: All {encrypt_type} encryption options enabled")
    if "plugin" in encrypt_type:
        test_helper.mysqld_options = f"{server_opts} --binlog-encryption"
    else:
        test_helper.mysqld_options = f"{server_opts} --binlog-encryption"
    test_helper.backup_params = f"{pxb_opts} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}" if pxb_comp else f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.initialize_db()
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True)

    # Sub-test 3: transition-key
    print(f"Test: {encrypt_type} with transition-key")
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    if "plugin" in encrypt_type:
        if test_helper.install_type == "package":
            test_helper.prepare_params = f"--transition-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
        else:
            test_helper.prepare_params = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin --transition-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
        plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key --early-plugin-load={plugin_name} {CORE_FILE_OPT}"
    elif pxb_comp:
        test_helper.prepare_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} {pxb_comp} {CORE_FILE_OPT}"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key {pxb_comp} {CORE_FILE_OPT}"
    else:
        test_helper.prepare_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
        test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key {CORE_FILE_OPT}"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Sub-test 4: generate-transition-key
    print(f"Test: {encrypt_type} with generate-transition-key")
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --generate-transition-key {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}" if pxb_comp else f"{pxb_opts} {CORE_FILE_OPT}"
    if pxb_comp:
        test_helper.restore_params = f"{pxb_opts} {pxb_comp} --generate-new-master-key {CORE_FILE_OPT}"
    else:
        if "plugin" in encrypt_type:
            plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
            test_helper.restore_params = f"{pxb_opts} --generate-new-master-key --early-plugin-load={plugin_name} {CORE_FILE_OPT}"
        else:
            test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Sub-test 5: lz4 compression with streaming
    print(f"Test: {encrypt_type} with lz4 compression and streaming")
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress=lz4 --compress-threads=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}" if pxb_comp else f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # Sub-test 6: zstd compression with streaming
    print(f"Test: {encrypt_type} with zstd compression and streaming")
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress=zstd --compress-threads=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}" if pxb_comp else f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # Sub-test 7+: DDL tests with encryption
    print(f"Test: DDL sub-tests with {encrypt_type}")
    if pxb_comp:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl {CORE_FILE_OPT}"
        test_helper.prepare_params = f"{pxb_opts} {pxb_comp} {CORE_FILE_OPT}"
    else:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl {CORE_FILE_OPT}"
        test_helper.prepare_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
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
    if test_helper.server_version_normalized >= 80000:
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
    test_helper.backup_params = f"{pxb_opts} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.initialize_db()
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True)

    # Transition-key test
    orig_lock_ddl = test_helper.lock_ddl
    test_helper.lock_ddl = "on"
    test_helper.backup_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    if test_helper.install_type == "package":
        test_helper.prepare_params = f"--transition-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
    else:
        test_helper.prepare_params = f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin --transition-key={test_helper.encrypt_key} {CORE_FILE_OPT}"
    plugin_name = "keyring_file.so" if "file" in encrypt_type else "keyring_vault.so"
    test_helper.restore_params = f"{pxb_opts} --transition-key={test_helper.encrypt_key} --generate-new-master-key --early-plugin-load={plugin_name} {CORE_FILE_OPT}"
    test_helper.take_backup(single_incremental=True)
    test_helper.lock_ddl = orig_lock_ddl

    # Streaming with compression
    test_helper.backup_params = (
        f"{pxb_opts} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K --compress --compress-threads=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.prepare_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.take_backup(backup_type="stream", single_incremental=True)

    # DDL sub-tests
    if test_helper.server_type == "MS":
        test_helper.backup_params = f"{pxb_opts} --lock-ddl-per-table {CORE_FILE_OPT}"
    else:
        test_helper.backup_params = f"{pxb_opts} --lock-ddl {CORE_FILE_OPT}"
        if encrypt_type == "keyring_file_plugin":
            no_binlog_opts = f"--early-plugin-load=keyring_file.so --keyring_file_data={test_helper.mysqldir}/keyring --innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt --innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"
            test_helper.mysqld_options = no_binlog_opts
            test_helper.initialize_db()
    test_helper.prepare_params = f"{pxb_opts} {CORE_FILE_OPT}"
    test_helper.restore_params = f"{pxb_opts} {CORE_FILE_OPT}"

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
    cloud_params = test_helper.build_cloud_params()

    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"--parallel=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    _run_load(test_helper)

    print("Test: Cloud backup")
    test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)

    print("Test: Cloud backup with encryption")
    test_helper.backup_params = (
        f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
        f"--encrypt-chunk-size=128K {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    )
    test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)

    if test_helper.server_version_normalized >= 80000:
        for compress in ["lz4", "zstd"]:
            print(f"Test: Cloud backup with {compress} compression and encryption")
            test_helper.backup_params = (
                f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} --encrypt-threads=10 "
                f"--encrypt-chunk-size=128K --compress={compress} --compress-threads=10 "
                f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
            )
            test_helper.take_backup(backup_type="cloud", cloud_params=cloud_params, single_incremental=True)


def test_cloud_backup_md5_delete(test_helper):
    """xbcloud put --md5: 'xbcloud delete' must also remove the .md5 sidecar.

    ``xbcloud put --md5`` uploads an additional ``<name>.md5`` object
    alongside a backup's chunk objects, for integrity verification.

    On PXB 8.4.0-6 (and earlier), ``xbcloud delete <name>`` removed every
    chunk object but left ``<name>.md5`` orphaned in the bucket forever,
    with no option to remove it afterwards -- confirmed by manual repro
    against a real backup/MinIO before writing this test. This was fixed in
    PXB 8.4.0-7, where ``xbcloud delete <name>`` removes the ``.md5`` object
    too. This test is a regression guard for that fix, and separately
    confirms that removing the ``.md5`` object does not affect ``xbcloud
    get``/restore of the backup (manual repro also confirmed this holds on
    the older, buggy build).
    """
    if test_helper.xtrabackup_version_normalized < test_helper.normalize_xtrabackup_version("8.4.0-7"):
        pytest.skip(
            f"xbcloud delete of --md5 sidecar requires PXB >= 8.4.0-7 "
            f"(found {test_helper.xtrabackup_version})"
        )
    cloud_params = test_helper.build_cloud_params()
    md5_cloud_params = f"{cloud_params} --md5"

    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"--parallel=10 {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    _run_load(test_helper, time_sec=10)
    while test_helper.is_load_running():
        time.sleep(1)

    rocksdb_enabled = test_helper.rocksdb == "enabled"
    databases = ["test", "test_rocksdb"] if rocksdb_enabled else ["test"]
    orig_data = test_helper.collect_table_data(databases)

    if os.path.exists(test_helper.backup_dir):
        shutil.rmtree(test_helper.backup_dir)
    os.makedirs(test_helper.backup_dir)

    log_date = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _take_cloud_md5_backup(backup_name: str, log_file: str) -> None:
        xb_cmd = test_helper._xtrabackup_cmd_prefix() + [
            "--no-defaults", f"--user={test_helper.backup_user}", "--password=",
            "--backup", f"--target-dir={os.path.join(test_helper.backup_dir, backup_name + '_tmp')}",
            f"-S{test_helper.socket_path}", f"--datadir={test_helper.datadir}",
            "--stream=xbstream",
        ] + test_helper.backup_params.split()
        pipe_cmd = (
            f"{' '.join(xb_cmd)} 2>{log_file} | "
            f"{os.path.join(test_helper.xtrabackup_dir, 'xbcloud')} put {md5_cloud_params} {backup_name} 2>>{log_file}"
        )
        result = subprocess.run(pipe_cmd, shell=True, check=False)
        if result.returncode != 0:
            pytest.fail(f"ERR: Cloud backup with 'put --md5' failed for '{backup_name}'. Log: {log_file}")

    # --- Part 1: 'xbcloud delete' must remove the .md5 sidecar too --------
    print("Test: 'xbcloud delete' must remove the .md5 object created by 'put --md5'")
    delete_backup_name = f"md5_delete_check_{log_date}"
    delete_log = os.path.join(test_helper.logdir, f"cloud_md5_delete_{log_date}_log")
    _take_cloud_md5_backup(delete_backup_name, delete_log)

    objects_before = test_helper.s3_list_objects(prefix=delete_backup_name)
    assert any(k.endswith(".md5") for k in objects_before), (
        f"Expected 'xbcloud put --md5' to create a .md5 object for '{delete_backup_name}'; "
        f"found none among: {objects_before}"
    )

    del_cmd = f"{os.path.join(test_helper.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {delete_backup_name} 2>>{delete_log}"
    result = subprocess.run(del_cmd, shell=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"ERR: xbcloud delete failed for '{delete_backup_name}'. Log: {delete_log}")

    leftover = test_helper.s3_list_objects(prefix=delete_backup_name)
    assert not leftover, (
        f"xbcloud delete left object(s) behind for '{delete_backup_name}': {leftover}. "
        "xbcloud delete is not removing the .md5 sidecar object created by 'put --md5' "
        "(reproduces on PXB 8.4.0-6 and earlier; fixed in PXB 8.4.0-7)."
    )

    # --- Part 2: removing the .md5 object must not affect get/restore -----
    print("Test: removing the .md5 object does not affect xbcloud get/restore")
    restore_backup_name = f"md5_restore_check_{log_date}"
    restore_log = os.path.join(test_helper.logdir, f"cloud_md5_restore_{log_date}_log")
    _take_cloud_md5_backup(restore_backup_name, restore_log)

    md5_objects = [k for k in test_helper.s3_list_objects(prefix=restore_backup_name) if k.endswith(".md5")]
    assert md5_objects, f"Expected a .md5 object for '{restore_backup_name}'; found none"
    for key in md5_objects:
        test_helper.s3_delete_object(key)
    remaining_md5 = [k for k in test_helper.s3_list_objects(prefix=restore_backup_name) if k.endswith(".md5")]
    assert not remaining_md5, f"Failed to remove .md5 object(s) before the download check: {remaining_md5}"

    full_target = os.path.join(test_helper.backup_dir, "full")
    if os.path.exists(full_target):
        shutil.rmtree(full_target)
    os.makedirs(full_target, exist_ok=True)
    get_cmd = (
        f"{os.path.join(test_helper.xtrabackup_dir, 'xbcloud')} get {cloud_params} {restore_backup_name} 2>>{restore_log} | "
        f"{os.path.join(test_helper.xtrabackup_dir, 'xbstream')} -xvC {full_target} 2>>{restore_log}"
    )
    result = subprocess.run(get_cmd, shell=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"ERR: xbcloud get failed after removing .md5 object(s). Log: {restore_log}")
    test_helper._decrypt_decompress(full_target, test_helper.backup_params)

    del_cmd = f"{os.path.join(test_helper.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {restore_backup_name} 2>>{restore_log}"
    subprocess.run(del_cmd, shell=True, check=False)

    test_helper.prepare_full_backup(test_helper.prepare_params, log_date)

    replica = test_helper.create_replica(
        name="md5_restore_check", server_id=110, port=18630,
        mysqld_options=test_helper.mysqld_options,
    )
    test_helper.restore_backup_to(replica.datadir, test_helper.restore_params, log_date)
    replica.start()
    for db in databases:
        replica.check_tables(database=db)

    replica_mysql = os.path.join(replica.basedir, "bin/mysql")
    for db in databases:
        for table, (orig_count, orig_cksum) in orig_data.get(db, {}).items():
            count_result = subprocess.run(
                [replica_mysql, "-uroot", f"-S{replica.socket_path}", "-BNe", f"SELECT COUNT(*) FROM {db}.{table}"],
                capture_output=True, text=True, check=False,
            )
            count = count_result.stdout.strip() if count_result.returncode == 0 else "ERR"
            cksum_result = subprocess.run(
                [replica_mysql, "-uroot", f"-S{replica.socket_path}", "-BNe", f"CHECKSUM TABLE {db}.{table}"],
                capture_output=True, text=True, check=False,
            )
            cksum_parts = cksum_result.stdout.strip().split() if cksum_result.returncode == 0 else []
            cksum = cksum_parts[1] if len(cksum_parts) >= 2 else "ERR"
            assert count == orig_count, f"{db}.{table}: row count mismatch after restore ({count} != {orig_count})"
            assert cksum == orig_cksum, f"{db}.{table}: checksum mismatch after restore ({cksum} != {orig_cksum})"

    print("Restore succeeded and data matched with the .md5 objects removed from cloud storage")


# ============================================================================
# InnoDB params and redo archive tests
# ============================================================================

def test_inc_backup_innodb_params(test_helper):
    """Backup and Restore with different InnoDB parameter values."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("InnoDB params tests require 8.0+")

    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.restore_params = ""

    configs = [
        {"mysqld": "--innodb-redo-log-capacity=209715200", "backup_extra": "--innodb-log-file-size=209715200",
         "prepare_extra": f"--innodb-log-file-size=209715200 {CORE_FILE_OPT}"},
        {"mysqld": "--innodb-redo-log-capacity=2147483648", "backup_extra": "--innodb-log-file-size=2147483648",
         "prepare_extra": f"--innodb-log-file-size=2147483648 {CORE_FILE_OPT}"},
        {"mysqld": "--innodb-redo-log-capacity=8388608 --innodb-buffer-pool-size=2G",
         "backup_extra": "--innodb-log-file-size=8388608 --innodb-buffer-pool-size=2G",
         "prepare_extra": f"--innodb-log-file-size=8388608 --innodb-buffer-pool-size=2G {CORE_FILE_OPT}"},
        {"mysqld": "--skip-log-bin", "backup_extra": "", "prepare_extra": f"{CORE_FILE_OPT}"},
    ]

    for cfg in configs:
        print(f"Test: InnoDB params: {cfg['mysqld']}")
        test_helper.mysqld_options = cfg["mysqld"]
        test_helper.backup_params = f"{cfg['backup_extra']} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}" if cfg["backup_extra"] else f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
        test_helper.prepare_params = cfg["prepare_extra"]
        test_helper.initialize_db()
        _run_load(test_helper)
        test_helper.take_backup(single_incremental=True)


def test_inc_backup_archive_log(test_helper):
    """Backup and Restore with redo archive log."""
    if test_helper.server_version_normalized < 80000:
        pytest.skip("Redo archive log tests require 8.0+")

    archive_dir = os.path.join(test_helper.mysqldir, "archive")
    os.makedirs(archive_dir, mode=0o744, exist_ok=True)

    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""

    test_helper.mysqld_options = (
        f"--innodb-extend-and-initialize=OFF --innodb-log-writer-threads=OFF "
        f"--innodb-redo-log-archive-dirs=archive:{archive_dir}"
    )
    test_helper.initialize_db()
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True)

    test_helper.mysqld_options = (
        f"--innodb-redo-log-capacity=536870912 --binlog-transaction-compression=ON "
        f"--binlog-transaction-compression-level-zstd=22 --innodb-extend-and-initialize=OFF "
        f"--innodb-log-writer-threads=OFF --innodb-redo-log-archive-dirs=archive:{archive_dir}"
    )
    test_helper.prepare_params = f"--innodb-log-file-size=536870912 {CORE_FILE_OPT}"
    test_helper.initialize_db()
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True)


# ============================================================================
# SSL tests
# ============================================================================

def test_ssl_backup(test_helper):
    """Backup and Restore with SSL options."""
    test_helper.mysqld_options = _default_mysqld_options()
    test_helper.backup_params = f"{CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=(test_helper.rocksdb == "enabled"))
    databases = ["test", "test_rocksdb"] if test_helper.rocksdb == "enabled" else ["test"]

    datadir = test_helper.datadir
    ssl_opts = f"--ssl-ca={datadir}/ca.pem --ssl-cert={datadir}/server-cert.pem --ssl-key={datadir}/server-key.pem"

    # Restart with SSL
    test_helper.stop_server()
    test_helper.mysqld_options += f" {ssl_opts}"
    test_helper.start_server()

    # Create backup user with SSL
    test_helper._run_sql("CREATE USER IF NOT EXISTS 'backup'@'localhost' REQUIRE SSL;")
    test_helper._run_sql("GRANT ALL ON *.* TO 'backup'@'localhost';")
    test_helper.backup_user = "backup"

    print("Test: Backup with SSL certificates and keys")
    test_helper.backup_params = f"{ssl_opts} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True, databases=databases)

    print("Test: Backup with --ssl-mode")
    port_result = subprocess.run(
        [os.path.join(test_helper.mysqldir, "bin/mysql"), "-uroot", f"-S{test_helper.socket_path}", "-Bse", "SELECT @@port;"],
        capture_output=True, text=True, check=False,
    )
    mysql_port = port_result.stdout.strip() if port_result.returncode == 0 else "21000"
    test_helper.backup_params = f"{ssl_opts} --ssl-mode=REQUIRED --host=127.0.0.1 -P {mysql_port} {CORE_FILE_OPT} --lock-ddl={test_helper.lock_ddl}"
    _run_load(test_helper)
    test_helper.take_backup(single_incremental=True, databases=databases)

    test_helper.backup_user = "root"


# ============================================================================
# Parallel processing order tests (PXB-3502)
# ============================================================================
#
# Before PXB-3502, xtrabackup's parallel worker pools (used by --decompress,
# --decrypt, --copy-back/--move-back and incremental --prepare) consumed a
# plain FIFO queue built by walking the backup directory in raw filesystem
# order. If the single largest file in the backup happened to be discovered
# late by that walk, one worker thread would still be grinding through it
# long after every other thread had run out of (smaller) work -- turning the
# largest file into an unnecessary long-tail straggler.
#
# PXB-3502 changed the underlying queue to a priority queue ordered by
# descending file size (ties broken by path, for determinism), so the
# largest file(s) are handed to worker threads first regardless of where
# they land in the directory walk.
#
# These tests build an adversarial dataset -- many tiny tables created first
# (so they're both alphabetically first and discovered first by a plain
# directory scan) followed by one much larger table created last (the worst
# case for a scheduler that ignores size) -- then check, from xtrabackup's
# own per-file dispatch log lines, how many small files were handed to a
# worker thread before the single largest file in the backup. A size-aware
# scheduler dispatches the largest file in its very first batch; a naive
# FIFO scheduler dispatches nearly everything else first.
#
# test_copy_back_largest_file_first always exercises InnoDB, and -- like the
# rest of this suite (e.g. `databases = ["test", "test_rocksdb"] if
# rocksdb_enabled else ["test"]`) -- folds RocksDB (MyRocks) coverage into the
# *same* test rather than a separate parametrized variant, gated on the
# global ROCKSDB setting:
#
#  - InnoDB: sysbench (oltp_insert.lua) 'prepare' builds NUM_SMALL_TABLES
#    tiny tables in SMALL_DB, then a single much bigger table in LARGE_DB
#    (created afterwards, so it sorts and is discovered last). Each table is
#    its own .ibd file, so "the largest file" is unambiguous.
#
#  - RocksDB (only when ROCKSDB=enabled): MyRocks has no per-table file --
#    all tables share one on-disk LSM-tree (a '.rocksdb' directory of
#    numbered .sst/.log files with no table correlation in their names), and
#    confirmed empirically that xtrabackup copies '.rocksdb' contents
#    through a *separate* pass of the same size-sorted queue (dispatched as
#    its own batch, after the ordinary per-table files). So instead of
#    naming a specific table's file, the RocksDB check stats every file
#    actually produced under '.rocksdb' and checks that the single largest
#    one (by real byte size, whichever file that turns out to be) is
#    dispatched first within that batch. Built with the same sysbench
#    'prepare' as InnoDB, just with --mysql-storage-engine=ROCKSDB. (If this
#    errors with "doesn't yet support 'InnoDB page COMPRESSION for the
#    RocksDB storage engine'", the environment's oltp_common.lua has been
#    hand-patched to force COMPRESSION='zlib' onto every CREATE TABLE --
#    `dpkg -V sysbench` will flag it; reinstall the package to fix.)
#
# Confirmed against real PXB builds before writing this test: reproduces on
# PXB 8.4.0-6 and earlier (for both InnoDB and RocksDB), and passes on PXB
# 8.4.0-7 and later, which ships the PXB-3502 fix.

# Number of small "decoy" tables created before the one large table. Kept
# well above PARALLEL_DEGREE so a FIFO scheduler has plenty of small work to
# hand out before it would ever reach the large table.
NUM_SMALL_TABLES = 30
SMALL_TABLE_ROWS = 50
LARGE_TABLE_ROWS = 100000
PARALLEL_DEGREE = 4

SMALL_DB = "aaa_small"
LARGE_DB = "zzz_large"
# Separate database names for the RocksDB dataset (built alongside the
# InnoDB one, only when ROCKSDB=enabled -- see test_copy_back_largest_file_first)
# so the two engines' sbtestN tables don't collide.
SMALL_DB_ROCKSDB = "aaa_small_rdb"
LARGE_DB_ROCKSDB = "zzz_large_rdb"
SYSBENCH_SCRIPT = "/usr/share/sysbench/oltp_insert.lua"

# Scopes the dispatch-log scan and the "largest file" search to only the
# files this test controls, engine-dependent: InnoDB tables land at
# <SMALL_DB|LARGE_DB>/sbtest<N>.ibd[.zst]; RocksDB tables share the single
# '.rocksdb/' LSM directory instead.
PARALLEL_ORDER_INNODB_SCOPE = re.compile(r"(?:" + re.escape(SMALL_DB) + r"|" + re.escape(LARGE_DB) + r")/")
PARALLEL_ORDER_ROCKSDB_SCOPE = re.compile(r"\.rocksdb/")


def _sysbench_prepare(
    test_helper, database: str, tables: int, table_size: int, log_name: str, storage_engine: Optional[str] = None
) -> None:
    """Create and populate sbtest1..sbtestN via sysbench 'prepare'."""
    log_path = os.path.join(test_helper.logdir, log_name)
    cmd = [
        "sysbench", SYSBENCH_SCRIPT,
        f"--tables={tables}", f"--table-size={table_size}",
        f"--mysql-db={database}", "--mysql-user=root", "--threads=10",
        "--db-driver=mysql", f"--mysql-socket={test_helper.socket_path}",
    ]
    if storage_engine:
        cmd.append(f"--mysql-storage-engine={storage_engine}")
    cmd.append("prepare")
    with open(log_path, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        pytest.fail(f"ERR: sysbench prepare for database '{database}' failed. Log: {log_path}")


def _build_adversarial_dataset(
    test_helper, rocksdb: bool = False, small_db: str = SMALL_DB, large_db: str = LARGE_DB
) -> None:
    """Build the small-tables/one-large-table dataset.

    small_db gets NUM_SMALL_TABLES tiny tables (sbtest1..sbtestN); large_db,
    created afterwards, gets a single much bigger sbtest1. large_db sorts
    alphabetically after small_db and is discovered after it by a plain
    directory scan -- the worst case for a scheduler that ignores size.
    """
    storage_engine = "ROCKSDB" if rocksdb else None
    test_helper.primary.mysql(f"CREATE DATABASE IF NOT EXISTS {small_db}")
    test_helper.primary.mysql(f"CREATE DATABASE IF NOT EXISTS {large_db}")
    _sysbench_prepare(
        test_helper, small_db, NUM_SMALL_TABLES, SMALL_TABLE_ROWS,
        f"adv_sysbench_{small_db}_prepare.log", storage_engine=storage_engine,
    )
    _sysbench_prepare(
        test_helper, large_db, 1, LARGE_TABLE_ROWS,
        f"adv_sysbench_{large_db}_prepare.log", storage_engine=storage_engine,
    )


def _dispatch_rank_of_largest(target_dir: str, log_path: str, dispatch_pattern: str, scope_pattern: "re.Pattern") -> tuple:
    """Find the single largest real file under target_dir whose relative
    path matches scope_pattern, then return its 1-based dispatch rank among
    log lines that match both dispatch_pattern and scope_pattern (i.e. how
    many other in-scope files were dispatched to a worker thread before it).

    Returns (rank, total_in_scope, largest_relpath, largest_size). rank is
    None if the largest file's line never appears in the log.
    """
    largest_relpath = None
    largest_size = -1
    for root, _dirs, files in os.walk(target_dir):
        for name in files:
            relpath = os.path.relpath(os.path.join(root, name), target_dir)
            if not scope_pattern.search(relpath):
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if size > largest_size:
                largest_size = size
                largest_relpath = relpath

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = [
            line for line in f
            if re.search(dispatch_pattern, line) and scope_pattern.search(line)
        ]

    rank = None
    for idx, line in enumerate(lines, start=1):
        if largest_relpath and largest_relpath in line:
            rank = idx
            break
    return rank, len(lines), largest_relpath, largest_size


def test_decompress_largest_file_first(test_helper):
    """PXB-3502: 'xtrabackup --decompress --parallel=N' must dispatch the
    largest compressed file to a worker thread before the bulk of the small
    decoy tables, not after them.
    """
    if test_helper.xtrabackup_version_normalized < test_helper.normalize_xtrabackup_version("8.4.0-7"):
        pytest.skip(
            f"PXB-3502 size-ordered decompress requires PXB >= 8.4.0-7 "
            f"(found {test_helper.xtrabackup_version})"
        )
    test_helper.mysqld_options = ""
    test_helper.backup_params = f"--compress --compress-threads={PARALLEL_DEGREE} {CORE_FILE_OPT}"
    test_helper.initialize_db()
    _build_adversarial_dataset(test_helper)

    if os.path.exists(test_helper.backup_dir):
        shutil.rmtree(test_helper.backup_dir)
    os.makedirs(test_helper.backup_dir)
    full_target = os.path.join(test_helper.backup_dir, "full")

    log_date = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_log = os.path.join(test_helper.logdir, f"adv_backup_{log_date}_log")
    xb_cmd = test_helper._xtrabackup_cmd_prefix() + [
        "--no-defaults", f"--user={test_helper.backup_user}", "--password=",
        "--backup", f"--target-dir={full_target}",
        f"-S{test_helper.socket_path}", f"--datadir={test_helper.datadir}",
    ] + test_helper.backup_params.split()
    result = test_helper.run_command(xb_cmd, check=False, log_file=backup_log)
    if result.returncode != 0:
        pytest.fail(f"ERR: Compressed backup failed. Log: {backup_log}")

    decompress_log = os.path.join(test_helper.logdir, f"adv_decompress_{log_date}_log")
    cmd = test_helper._xtrabackup_cmd_prefix() + [
        "--no-defaults", "--decompress", f"--target-dir={full_target}", f"--parallel={PARALLEL_DEGREE}",
    ]
    result = test_helper.run_command(cmd, check=False, log_file=decompress_log)
    if result.returncode != 0:
        pytest.fail(f"ERR: Decompress failed. Log: {decompress_log}")

    rank, total, largest, size = _dispatch_rank_of_largest(
        full_target, decompress_log, r"decompressing ", PARALLEL_ORDER_INNODB_SCOPE
    )
    assert rank is not None, f"largest in-scope file '{largest}' never appeared in the decompress dispatch log: {decompress_log}"
    assert rank == 1, (
        f"{rank - 1}/{total} smaller files were decompressed before the largest one "
        f"('{largest}', {size} bytes) (--parallel={PARALLEL_DEGREE}). Expected the largest file "
        f"to be dispatched first, ahead of everything smaller (PXB-3502); log: {decompress_log}"
    )


def test_copy_back_largest_file_first(test_helper):
    """PXB-3502: 'xtrabackup --copy-back --parallel=N' must dispatch the
    largest data file to a worker thread before the bulk of the small decoy
    tables, not after them.

    Always exercises InnoDB's per-table files. When ROCKSDB=enabled, the
    same backup/copy-back cycle additionally carries a RocksDB dataset, and
    RocksDB's shared '.rocksdb' checkpoint files are checked too -- one test,
    with scope extended by the global ROCKSDB setting, matching how the rest
    of this suite folds RocksDB in (e.g. ``databases = ["test", "test_rocksdb"]
    if rocksdb_enabled else ["test"]``) rather than a separate parametrized
    variant.
    """
    if test_helper.xtrabackup_version_normalized < test_helper.normalize_xtrabackup_version("8.4.0-7"):
        pytest.skip(
            f"PXB-3502 size-ordered copy-back requires PXB >= 8.4.0-7 "
            f"(found {test_helper.xtrabackup_version})"
        )
    rocksdb_enabled = test_helper.rocksdb == "enabled"

    test_helper.mysqld_options = ""
    test_helper.backup_params = f"{CORE_FILE_OPT}"
    test_helper.prepare_params = f"{CORE_FILE_OPT}"
    test_helper.restore_params = ""
    test_helper.initialize_db(rocksdb=rocksdb_enabled)
    _build_adversarial_dataset(test_helper)
    if rocksdb_enabled:
        _build_adversarial_dataset(test_helper, rocksdb=True, small_db=SMALL_DB_ROCKSDB, large_db=LARGE_DB_ROCKSDB)

    if os.path.exists(test_helper.backup_dir):
        shutil.rmtree(test_helper.backup_dir)
    os.makedirs(test_helper.backup_dir)
    full_target = os.path.join(test_helper.backup_dir, "full")

    log_date = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_log = os.path.join(test_helper.logdir, f"adv_backup_{log_date}_log")
    xb_cmd = test_helper._xtrabackup_cmd_prefix() + [
        "--no-defaults", f"--user={test_helper.backup_user}", "--password=",
        "--backup", f"--target-dir={full_target}",
        f"-S{test_helper.socket_path}", f"--datadir={test_helper.datadir}",
    ] + test_helper.backup_params.split()
    result = test_helper.run_command(xb_cmd, check=False, log_file=backup_log)
    if result.returncode != 0:
        pytest.fail(f"ERR: Backup failed. Log: {backup_log}")

    test_helper.prepare_full_backup(test_helper.prepare_params, log_date)

    restore_dir = os.path.join(test_helper.backup_dir, "restored_datadir")
    if os.path.exists(restore_dir):
        shutil.rmtree(restore_dir)
    os.makedirs(restore_dir)

    copyback_log = os.path.join(test_helper.logdir, f"adv_copyback_{log_date}_log")
    cmd = test_helper._xtrabackup_cmd_prefix() + [
        "--no-defaults", "--copy-back", f"--target-dir={full_target}",
        f"--datadir={restore_dir}", f"--parallel={PARALLEL_DEGREE}",
    ] + test_helper.restore_params.split()
    result = test_helper.run_command(cmd, check=False, log_file=copyback_log)
    if result.returncode != 0:
        pytest.fail(f"ERR: Copy-back failed. Log: {copyback_log}")

    rank, total, largest, size = _dispatch_rank_of_largest(full_target, copyback_log, r"\] Copying ", PARALLEL_ORDER_INNODB_SCOPE)
    assert rank is not None, f"largest in-scope file '{largest}' never appeared in the copy-back dispatch log: {copyback_log}"
    assert rank == 1, (
        f"{rank - 1}/{total} smaller InnoDB files were copied back before the largest one "
        f"('{largest}', {size} bytes) (--parallel={PARALLEL_DEGREE}). Expected the largest file "
        f"to be dispatched first, ahead of everything smaller (PXB-3502); log: {copyback_log}"
    )

    if rocksdb_enabled:
        rdb_rank, rdb_total, rdb_largest, rdb_size = _dispatch_rank_of_largest(
            full_target, copyback_log, r"\] Copying ", PARALLEL_ORDER_ROCKSDB_SCOPE
        )
        assert rdb_rank is not None, f"largest in-scope file '{rdb_largest}' never appeared in the copy-back dispatch log: {copyback_log}"
        assert rdb_rank == 1, (
            f"{rdb_rank - 1}/{rdb_total} smaller RocksDB files were copied back before the largest one "
            f"('{rdb_largest}', {rdb_size} bytes) (--parallel={PARALLEL_DEGREE}). Expected the largest file "
            f"to be dispatched first, ahead of everything smaller (PXB-3502); log: {copyback_log}"
        )

    # Functional guard: reordering the queue must not skip, duplicate, or
    # corrupt any file.
    for i in range(1, NUM_SMALL_TABLES + 1):
        restored = os.path.join(restore_dir, SMALL_DB, f"sbtest{i}.ibd")
        backed_up = os.path.join(full_target, SMALL_DB, f"sbtest{i}.ibd")
        assert os.path.isfile(restored), f"sbtest{i}.ibd missing from restored datadir: {restored}"
        assert os.path.getsize(restored) == os.path.getsize(backed_up), f"sbtest{i}.ibd size mismatch after copy-back"
    restored_large = os.path.join(restore_dir, LARGE_DB, "sbtest1.ibd")
    backed_up_large = os.path.join(full_target, LARGE_DB, "sbtest1.ibd")
    assert os.path.isfile(restored_large), f"{LARGE_DB}/sbtest1.ibd missing from restored datadir: {restored_large}"
    assert os.path.getsize(restored_large) == os.path.getsize(backed_up_large), (
        f"{LARGE_DB}/sbtest1.ibd size mismatch after copy-back"
    )
    if rocksdb_enabled:
        restored_rocksdb = os.path.join(restore_dir, ".rocksdb")
        assert os.path.isdir(restored_rocksdb) and os.listdir(restored_rocksdb), (
            f".rocksdb missing or empty in restored datadir: {restored_rocksdb}"
        )


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
            "Parallel_processing_order_tests",
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
        print("   export TEST_BASE_DIR=$HOME/innodb_myrocks_backup_tests")
        print("   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.0/bin")
        print("   export MYSQLDIR=$HOME/Percona-Server-8.0")
        print("   export QASCRIPTS=$HOME/server-qa")
        print("   export LOAD_TOOL=sysbench")
        print("   export ROCKSDB=enabled  # or disabled")
        print("   # Cloud (S3) backup test variables (required for test_cloud_inc_backup):")
        print("   export S3_BUCKET=<your-bucket>")
        print("   export S3_ACCESS_KEY=<your-access-key-id>")
        print("   export S3_SECRET_KEY=<your-secret-access-key>")
        print("   export S3_REGION=us-west-2")
        print("   export S3_ENDPOINT=https://s3.us-west-2.amazonaws.com")
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
        print("   Parallel_processing_order_tests")
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
        "Cloud_backup_tests": ["test_cloud_inc_backup", "test_cloud_backup_md5_delete"],
        "Innodb_params_redo_archive_tests": [
            "test_inc_backup_innodb_params",
            "test_inc_backup_archive_log",
        ],
        "SSL_tests": ["test_ssl_backup"],
        "Parallel_processing_order_tests": [
            "test_decompress_largest_file_first",
            "test_copy_back_largest_file_first",
        ],
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
