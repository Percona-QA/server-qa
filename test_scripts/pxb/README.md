# PXB incremental backup load tests

Tests in `inc_backup_load_tests.py` run backup/restore with a load tool (pquery/pstress/sysbench).  
**Assumption:** Percona Server (PS) and Percona XtraBackup (PXB) are already installed (e.g. from tarballs).

## Prerequisites

1. **Build the load tool** (e.g. pquery/pstress) with MySQL client libraries.
2. **Set environment variables** (or rely on script defaults):

   ```bash
   export TEST_BASE_DIR=$HOME/inc_backup_load_tests
   export XTRABACKUP_DIR=$HOME/percona-xtrabackup-8.0.35-34-Linux-x86_64.glibc2.35/bin
   export MYSQLDIR=$HOME/Percona-Server-8.0.44-35-Linux.x86_64.glibc2.35
   export QASCRIPTS=$HOME/server-qa
   export LOAD_TOOL=pstress
   export LOAD_TOOL_DIR=$HOME/lab/pstress/src
   ```

3. **Run from the `pxb` directory** so that `test_helper` and `kmip_helper` can be imported:

   ```bash
   cd $QASCRIPTS/test_scripts/pxb
   ```

Logs are written under `TEST_BASE_DIR` in test-specific subdirectories.

---

## How to run tests

Use **pytest** with the test file. Useful options:

- `-v` — verbose (recommended)
- `-s` — show stdout/stderr (no capture)
- `-k EXPR` — run tests whose name matches the expression

---

### Run a single non-parametrized test

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

### Run a single parametrized test (one variant)

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

**KMIP (one vault type):**  
Vault types come from `KMIP_CONFIGS` (e.g. `pykmip`, `fortanix`). Node id is the vault name:

```bash
pytest inc_backup_load_tests.py -v -s -k "test_kmip_component_backup[pykmip]"
```

To see all node ids:

```bash
pytest inc_backup_load_tests.py --collect-only -q
```

---

### Run two or three tests

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

### Run a specific test suite

The script defines logical **suites** and maps them to test names. You can run a suite either with the script’s CLI or by passing the same `-k` expression to pytest.

**Option A – script’s built-in suites (run from `pxb`):**

```bash
python inc_backup_load_tests.py Normal_and_Encryption_tests
python inc_backup_load_tests.py Kmip_Encryption_tests
python inc_backup_load_tests.py Rocksdb_tests
python inc_backup_load_tests.py Page_Tracking_tests
python inc_backup_load_tests.py Crash_tests
```

Verbose (no capture):

```bash
python inc_backup_load_tests.py -v Normal_and_Encryption_tests
```

**Option B – equivalent pytest `-k` for each suite:**

| Suite                         | Pytest -k equivalent |
|------------------------------|----------------------|
| Normal_and_Encryption_tests  | `test_normal_backup or test_keyring_plugin_backup or test_keyring_component_backup or test_memory_estimation_backup or test_crash_backup[innodb-no_pt]` |
| Kmip_Encryption_tests        | `test_kmip_component_backup` |
| Rocksdb_tests                | `test_rocksdb_backup or test_crash_backup[rocksdb-no_pt] or test_crash_backup[rocksdb-pt]` |
| Page_Tracking_tests          | `test_page_tracking_backup or test_crash_backup[innodb-pt] or test_crash_backup[rocksdb-pt]` |
| Crash_tests                  | `test_crash_backup` |

Example:

```bash
pytest inc_backup_load_tests.py -v -s -k "test_rocksdb_backup or test_crash_backup[rocksdb-no_pt] or test_crash_backup[rocksdb-pt]"
```

---

### Run all tests

Run the whole file with pytest:

```bash
pytest inc_backup_load_tests.py -v -s
```

To list all tests without running them:

```bash
pytest inc_backup_load_tests.py --collect-only -q
```

---

## Test reference

| Test                         | Type          | Notes |
|-----------------------------|---------------|--------|
| `test_normal_backup`        | Non-param     | Normal incremental backup/restore with load |
| `test_memory_estimation_backup` | Non-param | Memory estimation (sysbench only); skipped on 5.7 |
| `test_keyring_plugin_backup`    | Param `[no_pt]`, `[pt]` | keyring_file plugin |
| `test_keyring_component_backup` | Param `[no_pt]`, `[pt]` | keyring_file component |
| `test_rocksdb_backup`       | Non-param     | RocksDB; skipped on 5.7 and MS |
| `test_page_tracking_backup`| Non-param     | Page tracking; skipped on 5.7 |
| `test_crash_backup`         | Param         | `[innodb-no_pt]`, `[innodb-pt]`, `[rocksdb-no_pt]`, `[rocksdb-pt]` |
| `test_kmip_component_backup`| Param         | One id per vault in `KMIP_CONFIGS` (e.g. `[pykmip]`, `[fortanix]`) |
