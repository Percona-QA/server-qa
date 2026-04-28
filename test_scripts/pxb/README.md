# PXB Backup Tests

**Assumption:** Percona Server (PS) or MySQL Server (MS) and Percona XtraBackup (PXB) are already installed (e.g. from tarballs).

## Table of contents

- [Common prerequisites](#common-prerequisites)
- [inc\_backup\_load\_tests.py — Incremental backup load tests](#inc_backup_load_testspy--incremental-backup-load-tests)
  - [Additional environment variables](#additional-environment-variables)
  - [How to run tests](#how-to-run-tests)
    - [Run a single non-parametrized test](#run-a-single-non-parametrized-test)
    - [Run a single parametrized test (one variant)](#run-a-single-parametrized-test-one-variant)
    - [Run two or three tests](#run-two-or-three-tests)
    - [Run a specific test suite](#run-a-specific-test-suite)
    - [Run all tests](#run-all-tests)
  - [Test reference — inc\_backup\_load\_tests.py](#test-reference--inc_backup_load_testspy)
- [innodb\_myrocks\_backup\_tests.py — InnoDB/MyRocks backup tests](#innodb_myrocks_backup_testspy--innodbmyrocks-backup-tests)
  - [Additional environment variables](#additional-environment-variables-1)
  - [How to run tests](#how-to-run-tests-1)
    - [Run a single test](#run-a-single-test)
    - [Run a single parametrized encryption test](#run-a-single-parametrized-encryption-test)
    - [Run multiple tests](#run-multiple-tests)
    - [Run a specific test suite](#run-a-specific-test-suite-1)
    - [Run all tests](#run-all-tests-1)
  - [Test suites — innodb\_myrocks\_backup\_tests.py](#test-suites--innodb_myrocks_backup_testspy)
  - [Test reference — innodb\_myrocks\_backup\_tests.py](#test-reference--innodb_myrocks_backup_testspy)
- [replication\_backup\_tests.py — Replication + backup tests](#replication_backup_testspy--replication--backup-tests)
  - [Additional prerequisites](#additional-prerequisites)
  - [How to run tests](#how-to-run-tests-2)
    - [Run a single test](#run-a-single-test-1)
    - [Run multiple tests](#run-multiple-tests-1)
    - [Run a specific test suite](#run-a-specific-test-suite-2)
    - [Run all tests](#run-all-tests-2)
  - [Test reference — replication\_backup\_tests.py](#test-reference--replication_backup_testspy)
- [upgrade\_backup\_tests.py — Upgrade backup tests](#upgrade_backup_testspy--upgrade-backup-tests)
  - [Additional prerequisites](#additional-prerequisites-1)
  - [Additional environment variables](#additional-environment-variables-2)
  - [How to run tests](#how-to-run-tests-3)
    - [Run a single test](#run-a-single-test-2)
    - [Run multiple tests](#run-multiple-tests-2)
    - [Run a specific test suite](#run-a-specific-test-suite-3)
    - [Run all tests](#run-all-tests-3)
  - [Test reference — upgrade\_backup\_tests.py](#test-reference--upgrade_backup_testspy)

---

## Common prerequisites

1. **Set environment variables** (or rely on script defaults):

   ```bash
   export TEST_BASE_DIR=$HOME/inc_backup_load_tests
   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.0.35-34-Linux-x86_64.glibc2.35/bin
   export MYSQLDIR=$HOME/Percona-Server-8.0.44-35-Linux.x86_64.glibc2.35
   export QASCRIPTS=$HOME/server-qa
   ```

   **Optional:** To skip cleanup after each test (e.g. for debugging), set:

   ```bash
   export DISABLE_CLEANUP=1
   ```

   If `DISABLE_CLEANUP` is not set or is not `1`, cleanup runs as usual.

   **Optional:** Core dumps from `mysqld` and `xtrabackup` are disabled by default. To enable them (e.g. for debugging crashes), set:

   ```bash
   export ENABLE_CORE_DUMP=1
   ```

   When `ENABLE_CORE_DUMP` is unset or not `1`, the tests omit `--core-file` from all `mysqld` and `xtrabackup` commands. This applies to all Python test suites in this directory (`inc_backup_load_tests.py`, `innodb_myrocks_backup_tests.py`, `replication_backup_tests.py`).

2. **Run from the `pxb` directory** so that `test_helper` and `kmip_helper` can be imported:

   ```bash
   cd $QASCRIPTS/test_scripts/pxb
   ```

3. Logs are written under `TEST_BASE_DIR` in test-specific subdirectories.

---

## inc_backup_load_tests.py — Incremental backup load tests

Tests in `inc_backup_load_tests.py` run backup/restore with a load tool (pquery/pstress/sysbench).

### Additional environment variables

```bash
export LOAD_TOOL=pstress
export LOAD_TOOL_DIR=$HOME/lab/pstress/src
```

**For KMS encryption tests** (`test_kms_component_backup`), set:

```bash
export KMS_KEYID=<your-key-id>
export KMS_SECRET_KEY=<your-secret-key>
export KMS_AUTH_KEY=<your-auth-key>
export KMS_REGION=us-east-1
```

If any of these are unset, KMS tests are skipped. PS 8.0+ only; skipped on MS.

**For KMIP tests** (`test_kmip_component_backup`, `test_crash_backup_encrypted_kmip`):

- Vault types come from `KMIP_CONFIGS` in `test_helper.py` (currently `pykmip`, `fortanix`).
- KMIP tests require PS 8.0+ (skipped on 5.7); `keyring_kmip` tests are skipped on MS.
- For **Fortanix** vault variants, export:

```bash
export FORTANIX_EMAIL=<your-fortanix-email>
export FORTANIX_PASSWORD=<your-fortanix-password>
```

If Fortanix vars are not set, Fortanix-only variants are skipped.

### How to run tests

Use **pytest** with the test file. Useful options:

- `-v` — verbose (recommended)
- `-s` — show stdout/stderr (no capture)
- `-k EXPR` — run tests whose name matches the expression

---

#### Run a single non-parametrized test

Use the test function name with `-k` or as a node id:

```bash
pytest inc_backup_load_tests.py -v -s -k test_normal_backup
```

Other non-parametrized tests: `test_memory_estimation_backup`, `test_rocksdb_backup`, `test_page_tracking_backup`.

Example for another test:

```bash
pytest inc_backup_load_tests.py -v -s -k test_rocksdb_backup
```

---

#### Run a single parametrized test (one variant)

Parametrized tests have multiple variants; each variant has a **node id** like `test_name[id1-id2]`. Use the full node id.

**Keyring (page_tracking = no_pt | pt):**

```bash
# Keyring plugin – one variant
pytest inc_backup_load_tests.py -v -s -k "test_keyring_plugin_backup[pt]"

# Keyring component – one variant
pytest inc_backup_load_tests.py -v -s -k "test_keyring_component_backup[no_pt]"
```

**Crash tests (storage_engine + page_tracking):**
Ids: `innodb-no_pt`, `innodb-pt`, `rocksdb-no_pt`, `rocksdb-pt`

```bash
pytest inc_backup_load_tests.py -v -s -k "test_crash_backup[innodb-pt]"
```

**Encrypted crash tests (keyring_file):**
Ids: `no_pt`, `pt`

```bash
pytest inc_backup_load_tests.py -v -s -k "test_crash_backup_encrypted_keyring_file[pt]"
```

**Encrypted crash tests (keyring_kmip):**
Ids: one per `vault_type` x page tracking, e.g. `[pykmip-no_pt]`, `[pykmip-pt]`, `[fortanix-no_pt]`, `[fortanix-pt]`

```bash
pytest inc_backup_load_tests.py -v -s -k "test_crash_backup_encrypted_kmip[pykmip-pt]"
```

For `fortanix` variants, ensure `FORTANIX_EMAIL` and `FORTANIX_PASSWORD` are exported first.

**KMIP (one vault type):**
Vault types come from `KMIP_CONFIGS` (e.g. `pykmip`, `fortanix`). Node id is the vault name:

```bash
pytest inc_backup_load_tests.py -v -s -k "test_kmip_component_backup[pykmip]"
```

For `fortanix` variants, ensure `FORTANIX_EMAIL` and `FORTANIX_PASSWORD` are exported first.

**KMS (page_tracking = no_pt | pt):**
Requires `KMS_KEYID`, `KMS_SECRET_KEY`, `KMS_AUTH_KEY`, `KMS_REGION`. Node ids: `no_pt`, `pt`.

```bash
pytest inc_backup_load_tests.py -v -s -k "test_kms_component_backup[pt]"
```

To see all node ids:

```bash
pytest inc_backup_load_tests.py --collect-only -q
```

---

#### Run two or three tests

Use `-k` with **or** to match several tests (or parametrized variants):

```bash
# Two non-parametrized tests
pytest inc_backup_load_tests.py -v -s -k "test_normal_backup or test_rocksdb_backup"

# Three tests (mix of non-parametrized and parametrized)
pytest inc_backup_load_tests.py -v -s -k "test_normal_backup or test_page_tracking_backup or test_keyring_plugin_backup[no_pt]"

# Two parametrized variants
pytest inc_backup_load_tests.py -v -s -k "test_keyring_plugin_backup[no_pt] or test_keyring_plugin_backup[pt]"
```

---

#### Run a specific test suite

The script defines logical **suites** and maps them to test names. You can run a suite either with the script's CLI or by passing the same `-k` expression to pytest.

**Option A — script's built-in suites (run from `pxb`):**

```bash
python inc_backup_load_tests.py Normal_and_Encryption_tests
python inc_backup_load_tests.py Kmip_Encryption_tests
python inc_backup_load_tests.py Kms_Encryption_tests
python inc_backup_load_tests.py Rocksdb_tests
python inc_backup_load_tests.py Page_Tracking_tests
python inc_backup_load_tests.py Crash_tests
```

Verbose (no capture):

```bash
python inc_backup_load_tests.py -v Normal_and_Encryption_tests
```

**Option B — equivalent pytest `-k` for each suite:**

| Suite                         | Pytest -k equivalent |
|------------------------------|----------------------|
| Normal_and_Encryption_tests  | `test_normal_backup or test_keyring_plugin_backup or test_keyring_component_backup or test_memory_estimation_backup or test_crash_backup[innodb-no_pt] or test_crash_backup_encrypted_keyring_file` |
| Kmip_Encryption_tests        | `test_kmip_component_backup` |
| Kms_Encryption_tests         | `test_kms_component_backup` |
| Rocksdb_tests                | `test_rocksdb_backup or test_crash_backup[rocksdb-no_pt] or test_crash_backup[rocksdb-pt]` |
| Page_Tracking_tests          | `test_page_tracking_backup or test_crash_backup[innodb-pt] or test_crash_backup[rocksdb-pt] or test_crash_backup_encrypted_keyring_file[pt]` |
| Crash_tests                  | `test_crash_backup or test_crash_backup_encrypted_keyring_file or test_crash_backup_encrypted_kmip` |

Example:

```bash
pytest inc_backup_load_tests.py -v -s -k "test_rocksdb_backup or test_crash_backup[rocksdb-no_pt] or test_crash_backup[rocksdb-pt]"
```

---

#### Run all tests

Run the whole file with pytest:

```bash
pytest inc_backup_load_tests.py -v -s
```

To list all tests without running them:

```bash
pytest inc_backup_load_tests.py --collect-only -q
```

---

### Test reference — inc_backup_load_tests.py

| Test                         | Type          | Notes |
|-----------------------------|---------------|--------|
| `test_normal_backup`        | Non-param     | Normal incremental backup/restore with load |
| `test_memory_estimation_backup` | Non-param | Memory estimation (sysbench only); skipped on 5.7 |
| `test_keyring_plugin_backup`    | Param `[no_pt]`, `[pt]` | keyring_file plugin |
| `test_keyring_component_backup` | Param `[no_pt]`, `[pt]` | keyring_file component |
| `test_rocksdb_backup`       | Non-param     | RocksDB; skipped on 5.7 and MS |
| `test_page_tracking_backup`| Non-param     | Page tracking; skipped on 5.7 |
| `test_crash_backup`         | Param         | `[innodb-no_pt]`, `[innodb-pt]`, `[rocksdb-no_pt]`, `[rocksdb-pt]` |
| `test_crash_backup_encrypted_keyring_file` | Param `[no_pt]`, `[pt]` | Encrypted crash flow using keyring_file component |
| `test_crash_backup_encrypted_kmip` | Param | One id per `vault_type` x page tracking, e.g. `[pykmip-no_pt]`, `[pykmip-pt]`; Fortanix variants require `FORTANIX_EMAIL`, `FORTANIX_PASSWORD` |
| `test_kmip_component_backup`| Param         | One id per vault in `KMIP_CONFIGS` (e.g. `[pykmip]`, `[fortanix]`); Fortanix variants require `FORTANIX_EMAIL`, `FORTANIX_PASSWORD` |
| `test_kms_component_backup` | Param `[no_pt]`, `[pt]` | keyring_kms component; requires `KMS_KEYID`, `KMS_SECRET_KEY`, `KMS_AUTH_KEY`, `KMS_REGION`; skipped on 5.7 and MS |

---

## innodb_myrocks_backup_tests.py — InnoDB/MyRocks backup tests

Tests in `innodb_myrocks_backup_tests.py` run backup/restore during various DDL operations, with encryption (plugin and component keyrings), streaming, compression, cloud storage, SSL, and InnoDB parameter changes. Supports both InnoDB and MyRocks engines.

This is a Python rewrite of `innodb_myrocks_backup_tests.sh`.

### Additional environment variables

In addition to the [common prerequisites](#common-prerequisites), set:

```bash
export LOAD_TOOL=sysbench
export ROCKSDB=enabled          # or "disabled" (default: disabled)
export CLOUD_CONFIG=$HOME/aws.cnf
export INSTALL_TYPE=tarball     # or "package" (default: tarball)
```

**For cloud backup tests** (`test_cloud_inc_backup`), `CLOUD_CONFIG` must point to a valid xbcloud defaults file (e.g. `aws.cnf` with S3 credentials).

**For encryption tests**, the following are needed depending on the keyring type:

- **Vault** (`keyring_vault_plugin`, `keyring_vault_component`): requires a running Vault server; the test calls `vault_test_setup.sh` automatically.
- **KMIP** (`keyring_kmip_component`): requires KMIP server access; uses `kmip_helper.py` and `fortanix_kmip_setup.py`. For Fortanix variants, export `FORTANIX_EMAIL` and `FORTANIX_PASSWORD`.
- **KMS** (`keyring_kms_component`): requires AWS KMS credentials:

```bash
export KMS_KEYID=<your-key-id>
export KMS_SECRET_KEY=<your-secret-key>
export KMS_AUTH_KEY=<your-auth-key>
export KMS_REGION=us-east-1
```

### How to run tests

Use **pytest** with the test file. Useful options:

- `-v` — verbose (recommended)
- `-s` — show stdout/stderr (no capture)
- `-k EXPR` — run tests whose name matches the expression

---

#### Run a single test

```bash
pytest innodb_myrocks_backup_tests.py -v -s -k test_inc_backup
```

```bash
pytest innodb_myrocks_backup_tests.py -v -s -k test_streaming_backup
```

```bash
pytest innodb_myrocks_backup_tests.py -v -s -k test_ssl_backup
```

---

#### Run a single parametrized encryption test

Encryption tests are parametrized by keyring type. Node ids use the keyring name in brackets:

```bash
# 8.0+ keyring_file plugin
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_8_0[keyring_file_plugin]"

# 8.0+ keyring_vault component
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_8_0[keyring_vault_component]"

# 8.0+ keyring_kmip component
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_8_0[keyring_kmip_component]"

# 8.0+ keyring_kms component
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_8_0[keyring_kms_component]"

# 5.7 / PXB 2.4 keyring_file plugin
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_2_4[keyring_file_plugin]"

# 5.7 / PXB 2.4 keyring_vault plugin
pytest innodb_myrocks_backup_tests.py -v -s -k "test_encryption_2_4[keyring_vault_plugin]"
```

To see all node ids:

```bash
pytest innodb_myrocks_backup_tests.py --collect-only -q
```

---

#### Run multiple tests

Use `-k` with **or**:

```bash
pytest innodb_myrocks_backup_tests.py -v -s -k "test_add_drop_index or test_rename_index or test_change_compression"
```

---

#### Run a specific test suite

The script defines logical **suites** mapped to groups of tests. You can run a suite either with the script's CLI or by passing `-k` expressions to pytest.

**Option A — script's built-in suites (run from `pxb`):**

```bash
python innodb_myrocks_backup_tests.py Various_ddl_tests
python innodb_myrocks_backup_tests.py File_encrypt_compress_stream_tests
python innodb_myrocks_backup_tests.py Encryption_PXB8_0_PS8_0_tests
python innodb_myrocks_backup_tests.py Encryption_PXB9_0_PS9_0_tests
python innodb_myrocks_backup_tests.py Encryption_PXB8_0_PS8_0_KMIP_tests
python innodb_myrocks_backup_tests.py Encryption_PXB8_0_PS8_0_KMS_tests
python innodb_myrocks_backup_tests.py Encryption_PXB8_0_MS8_0_tests
python innodb_myrocks_backup_tests.py Encryption_PXB9_0_MS9_0_tests
python innodb_myrocks_backup_tests.py Encryption_PXB2_4_PS5_7_tests
python innodb_myrocks_backup_tests.py Encryption_PXB2_4_MS5_7_tests
python innodb_myrocks_backup_tests.py Cloud_backup_tests
python innodb_myrocks_backup_tests.py Innodb_params_redo_archive_tests
python innodb_myrocks_backup_tests.py SSL_tests
```

Verbose (no capture):

```bash
python innodb_myrocks_backup_tests.py -v Various_ddl_tests
```

Multiple suites at once:

```bash
python innodb_myrocks_backup_tests.py SSL_tests Cloud_backup_tests
```

**Option B — equivalent pytest `-k` for each suite:**

| Suite | Tests included |
|-------|---------------|
| `Various_ddl_tests` | `test_inc_backup`, `test_add_drop_index`, `test_rename_index`, `test_add_drop_full_text_index`, `test_change_index_type`, `test_spatial_data_index`, `test_add_drop_tablespace`, `test_change_compression`, `test_change_row_format`, `test_copy_data_across_engine`, `test_add_data_across_engine`, `test_update_truncate_table`, `test_create_drop_database`, `test_partitioned_tables`, `test_compressed_column`, `test_compression_dictionary`, `test_invisible_column`, `test_blob_column`, `test_add_drop_column_instant`, `test_add_drop_column_algorithms`, `test_run_all_statements` |
| `File_encrypt_compress_stream_tests` | `test_streaming_backup`, `test_compress_stream_backup`, `test_encrypt_compress_stream_backup`, `test_compress_backup` |
| `Encryption_PXB8_0_PS8_0_tests` | `test_encryption_8_0[keyring_file_plugin]`, `test_encryption_8_0[keyring_vault_plugin]`, `test_encryption_8_0[keyring_vault_component]`, `test_encryption_8_0[keyring_file_component]` |
| `Encryption_PXB9_0_PS9_0_tests` | `test_encryption_8_0[keyring_file_component]`, `test_encryption_8_0[keyring_vault_component]` |
| `Encryption_PXB8_0_PS8_0_KMIP_tests` | `test_encryption_8_0[keyring_kmip_component]` |
| `Encryption_PXB8_0_PS8_0_KMS_tests` | `test_encryption_8_0[keyring_kms_component]` |
| `Encryption_PXB8_0_MS8_0_tests` | `test_encryption_8_0[keyring_file_plugin]`, `test_encryption_8_0[keyring_file_component]` |
| `Encryption_PXB9_0_MS9_0_tests` | `test_encryption_8_0[keyring_file_component]` |
| `Encryption_PXB2_4_PS5_7_tests` | `test_encryption_2_4[keyring_file_plugin]`, `test_encryption_2_4[keyring_vault_plugin]` |
| `Encryption_PXB2_4_MS5_7_tests` | `test_encryption_2_4[keyring_file_plugin]` |
| `Cloud_backup_tests` | `test_cloud_inc_backup` |
| `Innodb_params_redo_archive_tests` | `test_inc_backup_innodb_params`, `test_inc_backup_archive_log` |
| `SSL_tests` | `test_ssl_backup` |

Example:

```bash
pytest innodb_myrocks_backup_tests.py -v -s -k "test_streaming_backup or test_compress_stream_backup or test_encrypt_compress_stream_backup or test_compress_backup"
```

---

#### Run all tests

Run the whole file with pytest:

```bash
pytest innodb_myrocks_backup_tests.py -v -s
```

To list all tests without running them:

```bash
pytest innodb_myrocks_backup_tests.py --collect-only -q
```

---

### Test suites — innodb_myrocks_backup_tests.py

| Suite | Description |
|-------|-------------|
| `Various_ddl_tests` | Backup/restore during DDL operations (index, tablespace, compression, row format, partitions, etc.) with concurrent background DDL |
| `File_encrypt_compress_stream_tests` | Streaming (xbstream, tar), lz4/zstd compression, and AES256 file-level encryption |
| `Encryption_PXB8_0_PS8_0_tests` | PXB 8.0 + PS 8.0 encryption with keyring_file (plugin/component) and keyring_vault (plugin/component) |
| `Encryption_PXB9_0_PS9_0_tests` | PXB 9.0 + PS 9.0 encryption with keyring_file and keyring_vault components |
| `Encryption_PXB8_0_PS8_0_KMIP_tests` | PXB 8.0 + PS 8.0 encryption with keyring_kmip component |
| `Encryption_PXB8_0_PS8_0_KMS_tests` | PXB 8.0 + PS 8.0 encryption with keyring_kms component (requires AWS KMS credentials) |
| `Encryption_PXB8_0_MS8_0_tests` | PXB 8.0 + MS 8.0 encryption with keyring_file (plugin/component) |
| `Encryption_PXB9_0_MS9_0_tests` | PXB 9.0 + MS 9.0 encryption with keyring_file component |
| `Encryption_PXB2_4_PS5_7_tests` | PXB 2.4 + PS 5.7 encryption with keyring_file and keyring_vault plugins |
| `Encryption_PXB2_4_MS5_7_tests` | PXB 2.4 + MS 5.7 encryption with keyring_file plugin |
| `Cloud_backup_tests` | Cloud incremental backup using xbcloud (requires `CLOUD_CONFIG`) |
| `Innodb_params_redo_archive_tests` | Backup with custom InnoDB parameters and redo log archiving |
| `SSL_tests` | Backup with SSL certificates, `--ssl-mode`, `--ssl-cipher`, and FIPS mode |

---

### Test reference — innodb_myrocks_backup_tests.py

| Test | Type | Notes |
|------|------|-------|
| `test_inc_backup` | Non-param | Basic incremental backup/restore |
| `test_add_drop_index` | Non-param | Backup during concurrent add/drop index |
| `test_rename_index` | Non-param | Backup during concurrent rename index |
| `test_add_drop_full_text_index` | Non-param | Backup during concurrent add/drop full-text index |
| `test_change_index_type` | Non-param | Backup during concurrent index type change |
| `test_spatial_data_index` | Non-param | Backup during concurrent add/drop spatial index |
| `test_add_drop_tablespace` | Non-param | Backup during concurrent add/drop tablespace |
| `test_change_compression` | Non-param | Backup during concurrent compression changes |
| `test_change_row_format` | Non-param | Backup during concurrent row format changes |
| `test_copy_data_across_engine` | Non-param | Cross-engine table copy (InnoDB to RocksDB); skipped if `ROCKSDB=disabled` |
| `test_add_data_across_engine` | Non-param | Concurrent data insert into InnoDB and RocksDB tables; skipped if `ROCKSDB=disabled` |
| `test_update_truncate_table` | Non-param | Backup during concurrent update/truncate |
| `test_create_drop_database` | Non-param | Backup during concurrent create/drop database |
| `test_partitioned_tables` | Non-param | Backup during concurrent partition operations |
| `test_compressed_column` | Non-param | Backup during concurrent column compression |
| `test_compression_dictionary` | Non-param | Backup during concurrent compression dictionary operations |
| `test_invisible_column` | Non-param | Backup during concurrent add/drop invisible columns |
| `test_blob_column` | Non-param | Backup during concurrent add/drop BLOB columns |
| `test_add_drop_column_instant` | Non-param | Backup during concurrent instant column add/drop |
| `test_add_drop_column_algorithms` | Non-param | Backup during concurrent column add/drop with various algorithms |
| `test_run_all_statements` | Non-param | Runs all DDL operations concurrently during backup |
| `test_streaming_backup` | Non-param | Streaming backup (xbstream); includes tar format on 5.7 |
| `test_compress_stream_backup` | Non-param | lz4/zstd compression with streaming; skipped on 5.7 |
| `test_encrypt_compress_stream_backup` | Non-param | AES256 encryption + lz4/zstd compression + streaming; skipped on 5.7 |
| `test_compress_backup` | Non-param | lz4/zstd compression without streaming; skipped on 5.7 |
| `test_encryption_8_0` | Param | `[keyring_file_plugin]`, `[keyring_vault_plugin]`, `[keyring_vault_component]`, `[keyring_file_component]`, `[keyring_kmip_component]`, `[keyring_kms_component]` — each runs multiple sub-tests (basic, all-options, transition-key, generate-transition-key, lz4/zstd streaming, DDL) |
| `test_encryption_2_4` | Param | `[keyring_file_plugin]`, `[keyring_vault_plugin]` — PXB 2.4 / PS 5.7 encryption tests |
| `test_cloud_inc_backup` | Non-param | Cloud incremental backup via xbcloud; requires `CLOUD_CONFIG` |
| `test_inc_backup_innodb_params` | Non-param | Backup with custom InnoDB parameters |
| `test_inc_backup_archive_log` | Non-param | Backup with redo log archiving; skipped on 5.7 |
| `test_ssl_backup` | Non-param | Backup with SSL certificates, ssl-mode, ssl-cipher, and FIPS mode |

---

## replication_backup_tests.py — Replication + backup tests

Tests in `replication_backup_tests.py` verify backup-based replication bootstrap using Percona XtraBackup. Each test initialises a primary server with a given `mysqld_options` configuration (GTID / no-GTID, multi / single-threaded replica, encryption) and then runs **two sub-scenarios**:

1. Create **`replica1`** from a backup taken on the primary.
2. Create **`replica2`** from a backup taken on `replica1` (using `--slave-info`).

After each replica starts, replication is configured, IO/SQL threads are asserted to be running, `CHECK TABLE` is run on the replica, and `pt-table-checksum` is run against the primary.

This is a Python rewrite of `replication_backup_tests.sh`. Unlike the bash script, primary and replicas share `MYSQLDIR` as their mysqld `--basedir`; only `--datadir` / `--socket` / `--port` / `--server-id` differ per instance, so no tarball copying is required.

Primary socket/datadir and the replica instances are managed by the `MySQLServer` class in `test_helper.py`; the helper exposes `helper.primary` and `helper.replicas: list[MySQLServer]` for lifecycle control.

### Additional prerequisites

In addition to the [common prerequisites](#common-prerequisites):

- **sysbench** must be installed (used for load generation on the primary).
- **percona-toolkit** must be installed (`pt-table-checksum` verifies primary/replica consistency).

If either dependency is missing, the entire module is skipped at collection time.

Recommended `TEST_BASE_DIR` override (keeps replication artefacts separate from other suites):

```bash
export TEST_BASE_DIR=$HOME/replication_backup_tests
```

The fixture creates per-test subdirectories under `TEST_BASE_DIR` for the primary datadir, each replica datadir, backups and logs.

### How to run tests

Use **pytest** with the test file. Useful options:

- `-v` — verbose (recommended)
- `-s` — show stdout/stderr (no capture)
- `-k EXPR` — run tests whose name matches the expression

---

#### Run a single test

All six tests are non-parametrized; run them by name with `-k`:

```bash
pytest replication_backup_tests.py -v -s -k test_replication_gtid_multithreaded
```

```bash
pytest replication_backup_tests.py -v -s -k test_replication_nogtid_singlethreaded
```

```bash
pytest replication_backup_tests.py -v -s -k test_replication_gtid_encryption
```

To see all collected tests without running them:

```bash
pytest replication_backup_tests.py --collect-only -q
```

---

#### Run multiple tests

Use `-k` with **or** to match several tests:

```bash
# Both GTID variants
pytest replication_backup_tests.py -v -s -k "test_replication_gtid_multithreaded or test_replication_gtid_singlethreaded"

# Both encryption variants
pytest replication_backup_tests.py -v -s -k "test_replication_gtid_encryption or test_replication_nogtid_encryption"
```

---

#### Run a specific test suite

The script defines logical **suites** and maps them to test names. You can run a suite either with the script's CLI or by passing the equivalent `-k` expression to pytest.

**Option A — script's built-in suites (run from `pxb`):**

```bash
python replication_backup_tests.py Gtid_tests
python replication_backup_tests.py NoGtid_tests
python replication_backup_tests.py Encryption_tests
python replication_backup_tests.py All
```

Verbose (no capture):

```bash
python replication_backup_tests.py -v Gtid_tests
```

Multiple suites at once:

```bash
python replication_backup_tests.py Gtid_tests Encryption_tests
```

**Option B — equivalent pytest `-k` for each suite:**

| Suite              | Pytest -k equivalent |
|--------------------|----------------------|
| `Gtid_tests`       | `test_replication_gtid_multithreaded or test_replication_gtid_singlethreaded` |
| `NoGtid_tests`     | `test_replication_nogtid_multithreaded or test_replication_nogtid_singlethreaded` |
| `Encryption_tests` | `test_replication_gtid_encryption or test_replication_nogtid_encryption` |
| `All`              | *(omit `-k` — run all tests in the file)* |

Example:

```bash
pytest replication_backup_tests.py -v -s -k "test_replication_gtid_multithreaded or test_replication_gtid_singlethreaded"
```

---

#### Run all tests

Run the whole file with pytest:

```bash
pytest replication_backup_tests.py -v -s
```

Or via the script CLI:

```bash
python replication_backup_tests.py All
```

---

### Test reference — replication_backup_tests.py

| Test | `mysqld_options` | Notes |
|------|-------------------|-------|
| `test_replication_gtid_multithreaded` | `GTID_OPTIONS --slave-parallel-workers=4` | GTID replication, multi-threaded applier |
| `test_replication_gtid_singlethreaded` | `GTID_OPTIONS --slave-parallel-workers={1 on 8.0+ else 0}` | GTID replication, single-threaded applier |
| `test_replication_nogtid_multithreaded` | `NO_GTID_OPTIONS --slave-parallel-workers=4` | Binlog file/pos replication, multi-threaded applier |
| `test_replication_nogtid_singlethreaded` | `NO_GTID_OPTIONS --slave-parallel-workers=0` | Binlog file/pos replication, single-threaded applier |
| `test_replication_gtid_encryption` | `GTID_OPTIONS` + keyring_file encryption | Uses `--keyring_file_data=<TEST_BASE_DIR>/.../keyring` and `--xtrabackup-plugin-dir`; 8.0+ uses `ENCRYPT_OPTIONS_8`, 5.7 uses `ENCRYPT_OPTIONS_57` |
| `test_replication_nogtid_encryption` | `NO_GTID_OPTIONS` + keyring_file encryption | Same as above but without GTID |

---

## upgrade_backup_tests.py — Upgrade backup tests

Tests in `upgrade_backup_tests.py` verify backup/prepare/restore compatibility between two PXB versions: a *previous* xtrabackup build and a *current* xtrabackup build. Each test runs the following sub-flows from `upgrade_backup_tests.sh`:

1. Take a full (or full + incremental) backup with one PXB version.
2. Prepare and restore with the other PXB version.
3. Restart the server, optionally apply binlog from the original datadir, run `CHECK TABLE`, and compare row counts before / after restore.

This is a Python rewrite of `upgrade_backup_tests.sh`. It re-uses `BackupTestHelper` from `test_helper.py` (`initialize_db`, `take_full_backup`, `take_incremental_backup`, `prepare_full_backup`, `restore_backup_to`, `count_rows`, `check_tables`, `check_dependencies`).

### Additional prerequisites

In addition to the [common prerequisites](#common-prerequisites):

- **sysbench** must be installed (used for the background load).
- **percona-toolkit** must be installed (`pt-table-checksum` is invoked by `check_pt_checksum`).
- Two PXB tarballs must be installed locally — one *previous* version and one *current* version.

### Additional environment variables

```bash
export TEST_BASE_DIR=$HOME/upgrade_backup_tests
export XTRABACKUP_DIR=$HOME/percona-xtrabackup-current/bin
export PREVIOUS_XTRABACKUP_DIR=$HOME/percona-xtrabackup-previous/bin
export MYSQLDIR=$HOME/Percona-Server-8.0.x-Linux.x86_64.glibc2.35
export QASCRIPTS=$HOME/server-qa
```

Optional:

```bash
export ROCKSDB=enabled    # default: disabled. When enabled, also creates and loads the
                          # test_rocksdb database (only valid on PS 8.0+)
```

`PREVIOUS_XTRABACKUP_DIR` is only consumed by this suite — it points at the older PXB `bin` directory used to take the source backups before they are prepared/restored with the current PXB pointed to by `XTRABACKUP_DIR`.

### How to run tests

Use **pytest** with the test file. Useful options:

- `-v` — verbose (recommended)
- `-s` — show stdout/stderr (no capture)
- `-k EXPR` — run tests whose name matches the expression

---

#### Run a single test

All three tests are non-parametrized; run them by name with `-k`:

```bash
pytest upgrade_backup_tests.py -v -s -k test_upgrade_full_backup
```

```bash
pytest upgrade_backup_tests.py -v -s -k test_upgrade_inc_backup
```

```bash
pytest upgrade_backup_tests.py -v -s -k test_upgrade_backup_encrypt
```

To see all collected tests without running them:

```bash
pytest upgrade_backup_tests.py --collect-only -q
```

---

#### Run multiple tests

Use `-k` with **or** to match several tests:

```bash
pytest upgrade_backup_tests.py -v -s -k "test_upgrade_full_backup or test_upgrade_inc_backup"
```

---

#### Run a specific test suite

The script defines logical **suites** mapped to individual tests. You can run a suite either with the script's CLI or by passing the equivalent `-k` expression to pytest.

**Option A — script's built-in suites (run from `pxb`):**

```bash
python upgrade_backup_tests.py Full_backup
python upgrade_backup_tests.py Inc_backup
python upgrade_backup_tests.py Encryption
python upgrade_backup_tests.py All
```

Verbose (no capture):

```bash
python upgrade_backup_tests.py -v Full_backup
```

Multiple suites at once:

```bash
python upgrade_backup_tests.py Full_backup Inc_backup
```

**Option B — equivalent pytest `-k` for each suite:**

| Suite          | Pytest -k equivalent             |
|----------------|----------------------------------|
| `Full_backup`  | `test_upgrade_full_backup`       |
| `Inc_backup`   | `test_upgrade_inc_backup`        |
| `Encryption`   | `test_upgrade_backup_encrypt`    |
| `All`          | *(omit `-k` — run all tests)*    |

Example:

```bash
pytest upgrade_backup_tests.py -v -s -k "test_upgrade_full_backup or test_upgrade_inc_backup"
```

---

#### Run all tests

Run the whole file with pytest:

```bash
pytest upgrade_backup_tests.py -v -s
```

Or via the script CLI:

```bash
python upgrade_backup_tests.py All
```

---

### Test reference — upgrade_backup_tests.py

| Test | Sub-scenarios run sequentially | Notes |
|------|--------------------------------|-------|
| `test_upgrade_full_backup` | 1. full backup with previous PXB → prepare/restore with current PXB | Plain `--log-bin=binlog` server options; sysbench load runs in background |
| `test_upgrade_inc_backup`  | 1. full + inc with previous PXB → prepare/restore with current PXB; 2. full with previous PXB + inc with current PXB → prepare/restore with current PXB | Two scenarios run sequentially against the same primary; sysbench load on each |
| `test_upgrade_backup_encrypt` | 1. full prev → prepare/restore current; 2. full + inc prev → prepare/restore current; 3. full prev + inc current → prepare/restore current | Auto-detects server type (PS / MS / 5.7) and applies matching keyring + encryption options; uses `--keyring_file_data=<MYSQLDIR>/keyring` and per-binary `--xtrabackup-plugin-dir` for both PXB binaries |
