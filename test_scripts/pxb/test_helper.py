#!/usr/bin/env python3
"""
Shared helper for PXB incremental backup load tests.
BackupTestHelper can be imported and used from inc_backup_load_tests.py and other scripts.
"""

import os
import sys
import subprocess
import time
import shutil
import re
import signal
import glob
import threading
from datetime import datetime
from typing import Optional, List, Tuple, Dict
import psutil
import pytest

# Import KMIP helper
try:
    from kmip_helper import KMIPHelper
except ImportError:
    KMIPHelper = None

# Set script variables
HOME = os.path.expanduser("~")
MYSQL_START_TIMEOUT = 60
TEST_BASE_DIR = os.environ.get("TEST_BASE_DIR", os.path.join(HOME, "inc_backup_load_tests"))
XTRABACKUP_DIR = os.environ.get("XTRABACKUP_DIR", os.path.join(HOME, "pxb-9.1/bld_9.1/install/bin"))
MYSQLDIR = os.environ.get("MYSQLDIR", os.path.join(HOME, "mysql-9.1/bld_9.1/install"))
QASCRIPTS = os.environ.get("QASCRIPTS", os.path.join(HOME, "server-qa"))
# DATADIR, BACKUP_DIR, and LOGDIR are now created per-test with test name included
# Optional: run xtrabackup under rr (record and replay). Set USE_RR=1 to enable.
USE_RR = os.environ.get("USE_RR", "0") == "1"

# Optional: enable core dumps for mysqld/xtrabackup. Set ENABLE_CORE_DUMP=1 to enable.
# When disabled, the --core-file flag is omitted from all mysqld and xtrabackup commands.
ENABLE_CORE_DUMP = os.environ.get("ENABLE_CORE_DUMP", "0") == "1"
CORE_FILE_OPT = "--core-file" if ENABLE_CORE_DUMP else ""

# KMIP Configurations
KMIP_CONFIGS = {
    "pykmip": "addr=127.0.0.1,image=satyapercona/kmip:latest,port=5696,name=kmip_pykmip",
    "fortanix": "addr=216.180.120.88,port=5696,name=kmip_fortanix,setup_script=fortanix_kmip_setup.py",
    # "hashicorp": "addr=127.0.0.1,port=5696,name=kmip_hashicorp,setup_script=hashicorp-kmip-setup.sh",
    # "ciphertrust": "addr=127.0.0.1,port=5696,name=kmip_ciphertrust,setup_script=setup_kmip_api.py",
}

# Set tool variables
LOAD_TOOL = os.environ.get("LOAD_TOOL", "pstress")  # Set value as pstress/sysbench
LOAD_TOOL_DIR = os.environ.get("LOAD_TOOL_DIR", os.path.join(HOME, "pstress_9.1/src"))  # pstress dir
NUM_TABLES = 25  # This will make 50 tables on the database tt_1, tt_1_p, .. tt_25, tt_25_p
TABLE_SIZE = 100
SECONDS = 60
THREADS = 5

# PXB Lock option
LOCK_DDL = "on"  # lock_ddl accepted values (on, reduced)

# Additional configuration from environment
CLOUD_CONFIG = os.environ.get("CLOUD_CONFIG", os.path.join(HOME, "aws.cnf"))
INSTALL_TYPE = os.environ.get("INSTALL_TYPE", "tarball")  # tarball or package
ROCKSDB = os.environ.get("ROCKSDB", "disabled")  # enabled or disabled
BACKUP_USER = os.environ.get("BACKUP_USER", "root")
ENCRYPT_KEY = os.environ.get("ENCRYPT_KEY", "mHU3Zs5sRcSB7zBAJP1BInPP5lgShKly")
RANDOM_TYPE = os.environ.get("RANDOM_TYPE", "uniform")

class MySQLServer:
    """A single mysqld instance (primary or replica).

    Owns per-instance state (datadir, socket_path, port, server_id, mysqld_options,
    pid, ...) and the per-instance lifecycle (initialize_datadir, start, stop,
    destroy, mysql, check_tables).  BackupTestHelper holds one primary and a list
    of replicas as instances of this class.
    """

    def __init__(
        self,
        name: str,
        datadir: str,
        socket_path: str,
        port: int,
        basedir: str,
        error_log: str,
        logdir: str,
        server_id: Optional[int] = None,
        mysqld_options: str = "",
        use_rr: bool = False,
        start_timeout: int = MYSQL_START_TIMEOUT,
        report_host: Optional[str] = None,
        tmpdir: Optional[str] = None,
        extra_start_args: Optional[List[str]] = None,
    ):
        self.name = name
        self.datadir = datadir
        self.socket_path = socket_path
        self.port = port
        self.basedir = basedir
        self.mysqldir = basedir  # Alias used when composing bin/mysql paths
        self.error_log = error_log
        self.logdir = logdir
        self.server_id = server_id
        self.mysqld_options = mysqld_options
        self.use_rr = use_rr
        self.start_timeout = start_timeout
        self.report_host = report_host
        self.tmpdir = tmpdir
        self.extra_start_args: List[str] = list(extra_start_args or [])
        self.pid: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize_datadir(self) -> None:
        """Wipe and (re)create ``datadir`` via ``mysqld --initialize-insecure``."""
        if os.path.exists(self.datadir):
            shutil.rmtree(self.datadir)

        os.makedirs(self.logdir, exist_ok=True)
        print(f"=>Creating data directory for '{self.name}'")
        log_file = os.path.join(self.logdir, f"{self.name}_install.log")
        with open(log_file, "w") as f:
            subprocess.run(
                [
                    os.path.join(self.basedir, "bin/mysqld"),
                    "--no-defaults",
                    f"--datadir={self.datadir}",
                    "--initialize-insecure",
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )
        print(f"..Data directory '{self.datadir}' created")

    def start(self, extra_args: Optional[List[str]] = None) -> None:
        """Launch this mysqld instance and wait until it answers ``mysqladmin ping``."""
        print(f"=>Starting MySQL server '{self.name}' on port {self.port}")
        mysqld_path = os.path.join(self.basedir, "bin/mysqld")
        cmd: List[str] = ["rr", mysqld_path] if self.use_rr else [mysqld_path]
        cmd += ["--no-defaults"]
        if ENABLE_CORE_DUMP:
            cmd.append("--core-file")
        cmd += [
            f"--basedir={self.basedir}",
            f"--datadir={self.datadir}",
        ]
        if self.tmpdir:
            cmd.append(f"--tmpdir={self.tmpdir}")
        cmd += self.mysqld_options.split()
        cmd += [
            f"--port={self.port}",
            f"--socket={self.socket_path}",
            f"--plugin-dir={self.basedir}/lib/plugin",
            f"--log-error={self.error_log}",
        ]
        if self.server_id is not None:
            cmd.append(f"--server-id={self.server_id}")
        if self.report_host is not None:
            cmd.append(f"--report-host={self.report_host}")
            cmd.append(f"--report-port={self.port}")
        cmd += list(self.extra_start_args)
        if extra_args:
            cmd += list(extra_args)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = process.pid

        mysqladmin_path = os.path.join(self.basedir, "bin/mysqladmin")
        for x in range(self.start_timeout + 1):
            time.sleep(1)
            try:
                result = subprocess.run(
                    [mysqladmin_path, "-uroot", f"-S{self.socket_path}", "ping"],
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    print(f"..Server '{self.name}' started successfully")
                    return
            except Exception:  # noqa: BLE001
                pass

            if x == self.start_timeout:
                pytest.fail(
                    f"ERR: Server '{self.name}' could not be started. "
                    f"Please check error log: {self.error_log}"
                )

    def stop(self, timeout: int = 300) -> None:
        """Gracefully shut down this instance, falling back to SIGTERM/SIGKILL."""
        if self.is_alive():
            result = subprocess.run(
                [
                    os.path.join(self.basedir, "bin/mysqladmin"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    f"--shutdown-timeout={timeout}",
                    "shutdown",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                print(f"..Server '{self.name}' shut down cleanly")
                self.pid = None
                return
            stderr_msg = (result.stderr or "").strip()
            print(f"Warning: mysqladmin shutdown on '{self.name}' returned exit code {result.returncode}")
            if stderr_msg:
                print(f"  stderr: {stderr_msg[:200]}")

        if self.pid:
            try:
                os.kill(self.pid, 0)
                print(f"  Server '{self.name}' pid={self.pid} still running, sending SIGTERM")
                os.kill(self.pid, signal.SIGTERM)
                for _ in range(30):
                    time.sleep(1)
                    try:
                        os.kill(self.pid, 0)
                    except OSError:
                        print(f"  Server '{self.name}' terminated after SIGTERM")
                        break
                else:
                    print(f"  Server '{self.name}' still running after SIGTERM, sending SIGKILL")
                    os.kill(self.pid, signal.SIGKILL)
                    time.sleep(2)
            except (ProcessLookupError, OSError):
                pass
            self.pid = None

    def destroy(self) -> None:
        """Stop the server and remove its data directory (used by cleanup)."""
        self.stop()
        if os.path.exists(self.datadir):
            print(f"=>Removing datadir for '{self.name}': {self.datadir}")
            shutil.rmtree(self.datadir, ignore_errors=True)

    def is_alive(self) -> bool:
        """Return True iff ``mysqladmin ping`` succeeds against this instance."""
        result = subprocess.run(
            [
                os.path.join(self.basedir, "bin/mysqladmin"),
                "-uroot",
                f"-S{self.socket_path}",
                "ping",
            ],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    # ------------------------------------------------------------------
    # SQL / utility helpers
    # ------------------------------------------------------------------

    def mysql(
        self,
        query: str,
        database: str = "",
        capture: bool = False,
        check: bool = True,
        user: str = "root",
    ) -> Optional[str]:
        """Run a single query via the ``mysql`` CLI client."""
        cmd = [os.path.join(self.basedir, "bin/mysql"), f"-u{user}", f"-S{self.socket_path}"]
        if database:
            cmd.append(database)
        cmd.extend(["-e", query])
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True, check=check)
            return result.stdout.strip() if result.returncode == 0 else None
        subprocess.run(cmd, capture_output=True, check=check)
        return None

    def check_tables(self, database: str = "test") -> None:
        """Run ``CHECK TABLE`` on every table in ``database``; ``pytest.fail`` on corruption."""
        print(f"Check the table status on '{self.name}' (db={database})")
        check_err = 0

        result = subprocess.run(
            [
                os.path.join(self.basedir, "bin/mysql"),
                "-uroot",
                f"-S{self.socket_path}",
                "-Bse",
                f"SHOW TABLES FROM {database};",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        tables = result.stdout.strip().split("\n")

        for table in tables:
            if not table:
                continue
            print(f"Checking table {table} ...")
            result = subprocess.run(
                [
                    os.path.join(self.basedir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-Bse",
                    f"CHECK TABLE {database}.{table}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                print(f"ERR: CHECK TABLE {database}.{table} query failed")
                if not self.is_alive():
                    pytest.fail(
                        f"ERR: Server '{self.name}' went down due to corruption in table {database}.{table}"
                    )

            table_status = result.stdout.strip()
            if "OK" not in table_status:
                print(f"ERR: CHECK TABLE {database}.{table} returned status '{table_status}'")
                check_err = 1

        if not self.is_alive():
            pytest.fail(
                f"ERR: Server '{self.name}' went down due to corruption, the restore was unsuccessful"
            )

        if check_err == 0:
            print("All innodb tables status: OK")
        else:
            print("After restore, some tables may be corrupt, check table status is not OK")

    def rewrite_option(self, pattern: str, replacement: str) -> None:
        """Regex-replace a substring in :attr:`mysqld_options` (e.g. for per-replica keyring paths)."""
        self.mysqld_options = re.sub(pattern, replacement, self.mysqld_options)


class BackupTestHelper:
    """Helper class for backup tests."""

    def __init__(
        self,
        xtrabackup_dir: str = XTRABACKUP_DIR,
        mysqldir: str = MYSQLDIR,
        datadir: Optional[str] = None,
        backup_dir: Optional[str] = None,
        logdir: Optional[str] = None,
        load_tool: str = LOAD_TOOL,
        load_tool_dir: str = LOAD_TOOL_DIR,
        num_tables: int = NUM_TABLES,
        table_size: int = TABLE_SIZE,
        seconds: int = SECONDS,
        threads: int = THREADS,
        lock_ddl: str = LOCK_DDL,
        test_name: Optional[str] = None,
    ):
        """Initialize test helper with configuration."""
        self.xtrabackup_dir = xtrabackup_dir
        self.mysqldir = mysqldir
        self.load_tool = load_tool
        self.load_tool_dir = load_tool_dir
        self.num_tables = num_tables
        self.table_size = table_size
        self.seconds = seconds
        self.threads = threads
        self.lock_ddl = lock_ddl
        self.mysql_start_timeout = MYSQL_START_TIMEOUT

        self.cloud_config = CLOUD_CONFIG
        self.install_type = INSTALL_TYPE
        self.rocksdb = ROCKSDB
        self.backup_user = BACKUP_USER
        self.encrypt_key = ENCRYPT_KEY
        self.random_type = RANDOM_TYPE
        self.qascripts = QASCRIPTS

        # Create test-specific directories with test name
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if test_name:
            # Use test name in directory names; sanitize for paths (e.g. parametrized: test_foo[bar] -> foo-bar)
            test_suffix = (
                test_name.replace("test_", "")
                .replace("_", "-")
                .replace("[", "-")
                .replace("]", "")
            )
            primary_datadir = datadir or os.path.join(TEST_BASE_DIR, f"data_{test_suffix}_{timestamp}")
            self.backup_dir = backup_dir or os.path.join(TEST_BASE_DIR, f"dbbackup_{test_suffix}_{timestamp}")
            self.logdir = logdir or os.path.join(TEST_BASE_DIR, f"backuplogs_{test_suffix}_{timestamp}")
            primary_socket = os.path.join(TEST_BASE_DIR, f"socket_{test_suffix}.sock")
        else:
            # Fallback to timestamp-only if no test name provided
            primary_datadir = datadir or os.path.join(TEST_BASE_DIR, f"data_{timestamp}")
            self.backup_dir = backup_dir or os.path.join(TEST_BASE_DIR, f"dbbackup_{timestamp}")
            self.logdir = logdir or os.path.join(TEST_BASE_DIR, f"backuplogs_{timestamp}")
            primary_socket = os.path.join(TEST_BASE_DIR, "socket.sock")

        # Primary MySQL server (shares basedir with MYSQLDIR).
        # Flags below preserve the exact command line produced by the previous
        # ``start_server`` implementation (max-connections, general-log,
        # log-error-verbosity) so existing tests behave identically.
        self.primary = MySQLServer(
            name="primary",
            datadir=primary_datadir,
            socket_path=primary_socket,
            port=21000,
            basedir=self.mysqldir,
            error_log=os.path.join(self.logdir, "error.log"),
            logdir=self.logdir,
            server_id=None,
            mysqld_options="",
            use_rr=USE_RR,
            start_timeout=MYSQL_START_TIMEOUT,
            report_host=None,
            tmpdir=None,
            extra_start_args=[
                "--max-connections=1024",
                "--general-log",
                "--log-error-verbosity=3",
            ],
        )
        self.replicas: List[MySQLServer] = []

        # Runtime variables
        self.server_type: Optional[str] = None
        self.version: Optional[str] = None
        self.version_normalized: Optional[int] = None
        self.pstress_binary: Optional[str] = None
        self.backup_params: str = ""
        self.prepare_params: str = ""
        self.restore_params: str = ""
        self.kmip_helper: Optional[KMIPHelper] = None

        # KMS configuration
        self.kms_region = os.environ.get("KMS_REGION", "us-east-1")
        self.kms_id = os.environ.get("KMS_KEYID", "")
        self.kms_auth_key = os.environ.get("KMS_AUTH_KEY", "")
        self.kms_secret_key = os.environ.get("KMS_SECRET_KEY", "")

        # Initialize paths
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}:{self.xtrabackup_dir}"

    # ------------------------------------------------------------------
    # Back-compat shims proxying to ``self.primary``
    # ------------------------------------------------------------------

    @property
    def socket_path(self) -> str:
        return self.primary.socket_path

    @property
    def datadir(self) -> str:
        return self.primary.datadir

    @property
    def mysql_pid(self) -> Optional[int]:
        return self.primary.pid

    @mysql_pid.setter
    def mysql_pid(self, value: Optional[int]) -> None:
        self.primary.pid = value

    @property
    def mysqld_options(self) -> str:
        return self.primary.mysqld_options

    @mysqld_options.setter
    def mysqld_options(self, value: str) -> None:
        self.primary.mysqld_options = value

    def _xtrabackup_cmd_prefix(self) -> List[str]:
        """Return command prefix for xtrabackup; prepend 'rr' when USE_RR=1."""
        xtrabackup_path = os.path.join(self.xtrabackup_dir, "xtrabackup")
        if USE_RR:
            return ["rr", xtrabackup_path]
        return [xtrabackup_path]

    @staticmethod
    def normalize_version(version_str: str) -> int:
        """Normalize version string to integer for comparison.
        
        Returns version as integer using zero-padded format:
        - 8.4.6 -> 80406
        - 8.0.0 -> 80000
        - 10.0.0 -> 100000
        - 10.5.3 -> 100503
        
        This matches the bash script's normalize_version function which uses
        printf %02d%02d%02d format. The format supports versions up to 99.99.99.
        """
        major = 0
        minor = 0
        patch = 0

        match = re.match(r"^(\d+)\.(\d+)\.?(\d*)([\.0-9])*$", version_str)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0

        # Return as integer: 8.4.6 -> 80406, 10.0.0 -> 100000
        # Format: %02d%02d%02d means 2 digits each, so max is 99.99.99
        return int(f"{major:02d}{minor:02d}{patch:02d}")

    def get_mysql_version(self) -> Tuple[str, int]:
        """Get MySQL version and normalized version. Also sets server_type from version output."""
        try:
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysqld"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            if self.server_type is None:
                self.server_type = "MS" if "MySQL Community Server" in result.stdout else "PS"
            version_match = re.search(r"Ver\s+([0-9]+\.[0-9]+[\.0-9]*)", result.stdout)
            if version_match:
                ver = version_match.group(1)
                normalized = self.normalize_version(ver)
                return ver, normalized
        except Exception as e:
            print(f"Error getting MySQL version: {e}")
        return "0.0.0", 0

    def get_mysql_type(self) -> str:
        """Get server type (PS or MS). Uses version output; does not require server to be running."""
        if self.server_type is None:
            self.get_mysql_version()
        return self.server_type or "PS"

    def check_pt_checksum(self):
        """Check PT Checksum tools compatibility."""
        if not shutil.which("pt-table-checksum"):
            pytest.fail("ERROR: pt-table-checksum is not installed")

        try:
            result = subprocess.run(
                ["pt-table-checksum", "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            pt_ver_match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            if pt_ver_match:
                pt_ver = pt_ver_match.group(1)
                pt_ver_norm = self.normalize_version(pt_ver)

                if (
                    self.version_normalized >= 80000
                    and self.version_normalized < 80400
                    and pt_ver_norm < self.normalize_version("3.0.9")
                ):
                    pytest.fail(
                        f"ERROR: MySQL 8.0 requires pt-table-checksum 3.0.9 or later (but found {pt_ver})"
                    )
                elif (
                    self.version_normalized >= 80400
                    and pt_ver_norm < self.normalize_version("3.7.0")
                ):
                    pytest.fail(
                        f"ERROR: MySQL 8.4 and higher versions requires pt-table-checksum 3.7.0 or later (but found {pt_ver})"
                    )
        except Exception as e:
            print(f"Warning: Could not check pt-table-checksum version: {e}")

    def run_command(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = True,
        log_file: Optional[str] = None,
        background: bool = False,
    ) -> subprocess.Popen:
        """Run a shell command."""
        if log_file:
            with open(log_file, "a") as f:
                f.write(f"Command: {' '.join(cmd)}\n")
                f.write(f"Time: {datetime.now()}\n")

        if background:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL if not log_file else open(log_file, "a"),
                stderr=subprocess.STDOUT,
            )
            return process
        else:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                check=check,
            )
            if log_file and capture_output:
                with open(log_file, "a", encoding="utf-8") as f:
                    if result.stdout:
                        f.write(result.stdout)
                    if result.stderr:
                        if result.stdout:
                            f.write("\n--- stderr ---\n")
                        f.write(result.stderr)
            return result

    def start_server(self):
        """Start MySQL primary server (thin delegator to ``self.primary.start``)."""
        self.primary.start()

    def stop_server(self, timeout: int = 300):
        """Stop MySQL primary server (thin delegator to ``self.primary.stop``)."""
        self.primary.stop(timeout=timeout)

    def run_mysql_query(
        self,
        query: str,
        database: str = "",
        check: bool = True,
        capture: bool = False,
    ) -> Optional[str]:
        """Run a MySQL query on the primary via the ``mysql`` CLI client."""
        return self.primary.mysql(
            query,
            database=database,
            capture=capture,
            check=check,
            user=self.backup_user,
        )

    # ------------------------------------------------------------------
    # Replica factory + replication primitives
    # ------------------------------------------------------------------

    def _replica_paths(self, name: str) -> Tuple[str, str, str]:
        """Return (datadir, socket_path, error_log) for a replica named ``name``.

        The replica's paths are derived from the primary's ``datadir`` /
        ``socket_path`` by appending ``_<name>`` / ``_<name>.sock`` so multiple
        replicas can coexist with the primary inside :data:`TEST_BASE_DIR`.
        """
        primary_datadir = self.primary.datadir
        primary_socket = self.primary.socket_path
        datadir = f"{primary_datadir}_{name}"
        socket_base, socket_ext = os.path.splitext(primary_socket)
        if not socket_ext:
            socket_ext = ".sock"
        socket_path = f"{socket_base}_{name}{socket_ext}"
        error_log = os.path.join(self.logdir, f"{name}.err")
        return datadir, socket_path, error_log

    def create_replica(
        self,
        name: str,
        server_id: int,
        port: int,
        mysqld_options: str = "",
        keyring_src: Optional[str] = None,
        tmpdir: Optional[str] = None,
    ) -> MySQLServer:
        """Create and register a new :class:`MySQLServer` replica.

        The replica shares the primary's ``basedir`` (``MYSQLDIR``) and only
        owns a distinct ``datadir`` / ``socket`` / ``port`` / ``server_id``.

        When ``keyring_src`` is provided and exists, the keyring file is copied
        into the replica's ``datadir`` and ``--keyring_file_data=`` in
        ``mysqld_options`` is rewritten to point at the per-replica copy.
        """
        datadir, socket_path, error_log = self._replica_paths(name)
        replica = MySQLServer(
            name=name,
            datadir=datadir,
            socket_path=socket_path,
            port=port,
            basedir=self.mysqldir,
            error_log=error_log,
            logdir=self.logdir,
            server_id=server_id,
            mysqld_options=mysqld_options,
            use_rr=USE_RR,
            start_timeout=MYSQL_START_TIMEOUT,
            report_host="127.0.0.1",
            tmpdir=tmpdir,
        )
        if keyring_src and os.path.exists(keyring_src):
            os.makedirs(datadir, exist_ok=True)
            dst = os.path.join(datadir, os.path.basename(keyring_src))
            shutil.copy(keyring_src, dst)
            replica.rewrite_option(r"--keyring_file_data=\S+", f"--keyring_file_data={dst}")
        self.replicas.append(replica)
        return replica

    def prepare_full_backup(self, prepare_params: str, log_date: str) -> None:
        """Run ``xtrabackup --prepare`` on ``<backup_dir>/full``."""
        print("=>Preparing full backup")
        target = os.path.join(self.backup_dir, "full")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--prepare",
            f"--target-dir={target}",
        ] + prepare_params.split()
        log_file = os.path.join(self.logdir, f"prepare_full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: xtrabackup --prepare failed. Please check the log at: {log_file}")
        print(f"..Prepared full backup at: {target}")

    def restore_backup_to(
        self,
        target_datadir: str,
        restore_params: str,
        log_date: str,
    ) -> None:
        """Run ``xtrabackup --copy-back`` from ``<backup_dir>/full`` into ``target_datadir``."""
        print(f"=>Restoring full backup to {target_datadir}")
        if os.path.exists(target_datadir):
            shutil.rmtree(target_datadir)
        os.makedirs(target_datadir, exist_ok=True)
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--copy-back",
            f"--target-dir={os.path.join(self.backup_dir, 'full')}",
            f"--datadir={target_datadir}",
        ] + restore_params.split()
        log_file = os.path.join(self.logdir, f"restore_backup_{os.path.basename(target_datadir)}_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: xtrabackup --copy-back failed. Please check the log at: {log_file}")
        print(f"..Restored datadir at: {target_datadir}")

    def take_backup_from(
        self,
        source: MySQLServer,
        extra_args: Optional[List[str]] = None,
        log_date: Optional[str] = None,
    ) -> None:
        """Take a full backup from ``source`` (any :class:`MySQLServer`) into ``<backup_dir>/full``.

        This is the multi-server counterpart of :meth:`take_full_backup`, used
        for replica chaining (backup from replica1 → restore into replica2).
        """
        if log_date is None:
            log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_target = os.path.join(self.backup_dir, "full")
        if os.path.exists(full_target):
            shutil.rmtree(full_target)
        os.makedirs(self.backup_dir, exist_ok=True)

        print(f"=>Taking full backup from '{source.name}'")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--user=root",
            "--password=",
            "--backup",
            f"--target-dir={full_target}",
            f"-S{source.socket_path}",
            f"--datadir={source.datadir}",
        ] + self.backup_params.split() + ["--register-redo-log-consumer"]
        if extra_args:
            cmd += list(extra_args)
        log_file = os.path.join(self.logdir, f"full_backup_from_{source.name}_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(
                f"ERR: Full backup from '{source.name}' failed. "
                f"Please check the log at: {log_file}"
            )
        print(f"..Full backup from '{source.name}' successfully created at: {full_target}")

    def configure_replication(
        self,
        replica: MySQLServer,
        master: MySQLServer,
        slave_info: bool = False,
    ) -> None:
        """Wire up ``replica`` to replicate from ``master`` using backup metadata.

        When ``slave_info`` is False, the restored backup's
        ``xtrabackup_binlog_info`` (master bin-log coordinates) is used.  When
        True, ``xtrabackup_slave_info`` (a prebuilt ``CHANGE MASTER TO`` or
        ``CHANGE REPLICATION SOURCE TO`` statement) is used, as produced by
        ``xtrabackup --slave-info``.

        The function dispatches ``RESET MASTER|BINARY LOGS AND GTIDS`` and
        ``RESET REPLICA|SLAVE`` as appropriate for the current server major
        version so it works on 5.7 as well as 8.0 / 8.4+.
        """
        version_normalized = self.version_normalized or 0
        use_replica_keyword = version_normalized >= 80400
        use_slave_keyword = version_normalized < 80400

        # RESET MASTER / BINARY LOGS AND GTIDS on the replica.
        if version_normalized >= 80400:
            replica.mysql("RESET BINARY LOGS AND GTIDS;")
        else:
            replica.mysql("RESET MASTER;")

        # RESET REPLICA / RESET SLAVE on the replica.
        if use_replica_keyword:
            replica.mysql("RESET REPLICA ALL;")
        else:
            replica.mysql("RESET SLAVE ALL;")

        if slave_info:
            slave_info_file = os.path.join(self.backup_dir, "full", "xtrabackup_slave_info")
            if not os.path.exists(slave_info_file):
                pytest.fail(
                    f"ERR: xtrabackup_slave_info not found in backup dir: {slave_info_file}"
                )
            with open(slave_info_file, "r") as f:
                change_master_stmt = f.read().strip().rstrip(";")
            change_master_stmt = change_master_stmt.replace(
                "CHANGE MASTER TO",
                "CHANGE MASTER TO MASTER_HOST='127.0.0.1', "
                f"MASTER_PORT={master.port}, MASTER_USER='root', "
                "MASTER_PASSWORD='',",
                1,
            )
            change_master_stmt = change_master_stmt.replace(
                "CHANGE REPLICATION SOURCE TO",
                "CHANGE REPLICATION SOURCE TO SOURCE_HOST='127.0.0.1', "
                f"SOURCE_PORT={master.port}, SOURCE_USER='root', "
                "SOURCE_PASSWORD='',",
                1,
            )
            replica.mysql(change_master_stmt + ";")
        else:
            binlog_info_file = os.path.join(self.backup_dir, "full", "xtrabackup_binlog_info")
            if not os.path.exists(binlog_info_file):
                pytest.fail(
                    f"ERR: xtrabackup_binlog_info not found in backup dir: {binlog_info_file}"
                )
            with open(binlog_info_file, "r") as f:
                parts = f.read().split()
            if len(parts) < 2:
                pytest.fail(
                    f"ERR: Unexpected xtrabackup_binlog_info contents: {parts!r}"
                )
            binlog_file, binlog_pos = parts[0], parts[1]
            if use_replica_keyword:
                stmt = (
                    f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='127.0.0.1', "
                    f"SOURCE_PORT={master.port}, SOURCE_USER='root', "
                    f"SOURCE_PASSWORD='', SOURCE_LOG_FILE='{binlog_file}', "
                    f"SOURCE_LOG_POS={binlog_pos};"
                )
            else:
                stmt = (
                    f"CHANGE MASTER TO MASTER_HOST='127.0.0.1', "
                    f"MASTER_PORT={master.port}, MASTER_USER='root', "
                    f"MASTER_PASSWORD='', MASTER_LOG_FILE='{binlog_file}', "
                    f"MASTER_LOG_POS={binlog_pos};"
                )
            replica.mysql(stmt)

        # START REPLICA / START SLAVE
        if use_replica_keyword:
            replica.mysql("START REPLICA;")
        else:
            replica.mysql("START SLAVE;")

        # Give the IO/SQL threads a moment to come up, then verify.
        time.sleep(2)
        if use_slave_keyword:
            status_query = "SHOW SLAVE STATUS\\G"
            io_key = "Slave_IO_Running"
            sql_key = "Slave_SQL_Running"
        else:
            status_query = "SHOW REPLICA STATUS\\G"
            io_key = "Replica_IO_Running"
            sql_key = "Replica_SQL_Running"

        status_output = replica.mysql(status_query, capture=True) or ""
        io_ok = any(
            line.strip().startswith(f"{io_key}:") and line.strip().endswith("Yes")
            for line in status_output.splitlines()
        )
        sql_ok = any(
            line.strip().startswith(f"{sql_key}:") and line.strip().endswith("Yes")
            for line in status_output.splitlines()
        )
        if not (io_ok and sql_ok):
            pytest.fail(
                f"ERR: Replication IO/SQL threads not running on '{replica.name}'.\n"
                f"{status_output}"
            )
        print(f"..Replication started on '{replica.name}' from '{master.name}'")

    def initialize_db(self, rocksdb: bool = False):
        """Initialize and start MySQL database. When rocksdb=True, also set up RocksDB engine and test_rocksdb database."""
        os.makedirs(self.logdir, exist_ok=True)
        self.primary.stop()
        self.primary.initialize_datadir()
        self.primary.start()

        # Load MyRocks SQL only when RocksDB is needed; loading it unconditionally
        # installs ha_rocksdb.so which causes xtrabackup to attempt RocksDB
        # checkpoint creation even when no RocksDB data exists.
        myrocks_sql = os.path.join(self.qascripts, "MyRocks.sql")
        if rocksdb and os.path.isfile(myrocks_sql):
            subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}"],
                stdin=open(myrocks_sql),
                capture_output=True,
                check=False,
            )

        # Drop and create test database
        subprocess.run(
            [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-e", "CREATE DATABASE IF NOT EXISTS test"],
            check=True,
        )

        # Determine server type
        result = subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysql"),
                "-uroot",
                f"-S{self.socket_path}",
                "-Ne",
                "SELECT COUNT(*) FROM information_schema.engines WHERE engine='InnoDB' AND comment LIKE 'Percona%';",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        output = result.stdout.strip()

        self.version, self.version_normalized = self.get_mysql_version()

        if output == "1":
            self.server_type = "PS"
            print(f"Test is running against: {self.server_type}-{self.version}")
            if self.load_tool == "pstress":
                self.pstress_binary = "pstress-ps"
                if not os.path.exists(os.path.join(self.load_tool_dir, "pstress-ps")):
                    pytest.fail("pstress-ps not found. Please compile pstress with Percona Server!")
        elif output == "0":
            self.server_type = "MS"
            print(f"Test is running against: {self.server_type}-{self.version}")
            if self.load_tool == "pstress":
                self.pstress_binary = "pstress-ms"
                if not os.path.exists(os.path.join(self.load_tool_dir, "pstress-ms")):
                    pytest.fail("pstress-ms not found. Please compile pstress with Percona Server!")
        else:
            pytest.fail("Invalid server version!")

        # Create data using sysbench
        if self.load_tool == "sysbench" or rocksdb:
            if "keyring" not in self.mysqld_options:
                subprocess.run(
                    [
                        "sysbench", "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}", f"--table-size={self.table_size}",
                        "--mysql-db=test", "--mysql-user=root", "--threads=100",
                        "--db-driver=mysql", f"--mysql-socket={self.socket_path}",
                        f"--rand-type={self.random_type}", "prepare",
                    ],
                    stdout=open(os.path.join(self.logdir, "sysbench_prepare.log"), "w"),
                    stderr=subprocess.STDOUT,
                    check=True,
                )

                if rocksdb:
                    print("Installing rocksdb storage engine")
                    subprocess.run(
                        [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}"],
                        stdin=open(myrocks_sql) if os.path.isfile(myrocks_sql) else subprocess.DEVNULL,
                        capture_output=True, check=False,
                    )
                    print("Creating rocksdb data in database")
                    subprocess.run(
                        [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-e",
                         "CREATE DATABASE IF NOT EXISTS test_rocksdb;"],
                        check=False,
                    )
                    subprocess.run(
                        [
                            "sysbench", "/usr/share/sysbench/oltp_insert.lua",
                            f"--tables={self.num_tables}", f"--table-size={self.table_size}",
                            "--mysql-db=test_rocksdb", "--mysql-user=root", "--threads=100",
                            "--db-driver=mysql", "--mysql-storage-engine=ROCKSDB",
                            f"--mysql-socket={self.socket_path}", f"--rand-type={self.random_type}", "prepare",
                        ],
                        stdout=open(os.path.join(self.logdir, "sysbench_rocksdb_prepare.log"), "w"),
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
            else:
                # Encryption enabled - create encrypted tables
                print("Creating encrypted tables in innodb")
                result = subprocess.run(
                    [
                        "sysbench", "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}", f"--table-size={self.table_size}",
                        "--mysql-db=test", "--mysql-user=root", "--threads=100",
                        "--db-driver=mysql", f"--mysql-socket={self.socket_path}",
                        '--mysql-table-options=Encryption=\'Y\'',
                        f"--rand-type={self.random_type}", "prepare",
                    ],
                    capture_output=True, check=False,
                )
                if result.returncode != 0:
                    for i in range(1, self.num_tables + 1):
                        print(f"Creating the table sbtest{i}...")
                        subprocess.run(
                            [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-e",
                             f"CREATE TABLE test.sbtest{i} (id int(11) NOT NULL AUTO_INCREMENT, k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y';"],
                            check=True,
                        )
                    print("Adding data in tables...")
                    subprocess.run(
                        [
                            "sysbench", "/usr/share/sysbench/oltp_insert.lua",
                            f"--tables={self.num_tables}", "--mysql-db=test", "--mysql-user=root",
                            "--threads=50", "--db-driver=mysql", f"--mysql-socket={self.socket_path}",
                            "--time=30", f"--rand-type={self.random_type}", "run",
                        ],
                        capture_output=True, check=False,
                    )

    def run_load(self, tool_options: str, database: str = "test", engine: Optional[str] = None, time_sec: Optional[int] = None):
        """Run a load using pstress/sysbench.

        Args:
            tool_options: pstress command-line options (ignored for sysbench).
            database: database name for sysbench (default "test").
            engine: if set, passes --mysql-storage-engine to sysbench (e.g. "ROCKSDB").
            time_sec: sysbench --time value; defaults to self.seconds.
        """
        if self.load_tool == "pstress":
            print(f"Run pstress with options: {tool_options}")
            cmd = [os.path.join(self.load_tool_dir, self.pstress_binary)] + tool_options.split()
            if self.lock_ddl == "reduced":
                cmd.extend(["--rotate-master-key", "0"])
            cmd.extend(
                [
                    f"--logdir={self.logdir}/pstress",
                    "--no-temp-tables",
                    f"--socket={self.socket_path}",
                ]
            )
            log_file = os.path.join(self.logdir, "pstress/pstress.log")
            self.run_command(cmd, check=False, background=True, log_file=log_file)
            time.sleep(2)
        else:
            run_time = time_sec if time_sec is not None else self.seconds
            print(f"Run sysbench on database={database} time={run_time}")
            cmd = [
                "sysbench", "/usr/share/sysbench/oltp_insert.lua",
                f"--tables={self.num_tables}",
                f"--mysql-db={database}",
                "--mysql-user=root",
                "--threads=50",
                "--db-driver=mysql",
                f"--mysql-socket={self.socket_path}",
                f"--time={run_time}",
                f"--rand-type={self.random_type}",
                "run",
            ]
            if engine:
                cmd.insert(-1, f"--mysql-storage-engine={engine}")
            log_file = os.path.join(self.logdir, f"sysbench_{database}.log")
            self.run_command(cmd, check=False, background=True, log_file=log_file)

    def is_load_running(self) -> bool:
        """Check if load tool is running."""
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if self.load_tool in proc.info["name"].lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def take_backup(
        self,
        backup_type: str = "",
        cloud_params: str = "",
        single_incremental: bool = False,
        databases: Optional[List[str]] = None,
    ):
        """Take incremental backup with full/inc/prepare/restore cycle.

        Args:
            backup_type: "" (normal), "stream", "tar", or "cloud".
            cloud_params: xbcloud options (used when backup_type="cloud").
            single_incremental: when True, take exactly one incremental then stop.
            databases: list of databases to verify (default ["test"]).
        """
        if databases is None:
            databases = ["test"]

        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        os.makedirs(self.backup_dir)

        log_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        use_detailed_verify = "test_rocksdb" in databases or not shutil.which("pt-table-checksum")

        # --- Full backup ---
        print("=>Taking full backup")
        full_target = os.path.join(self.backup_dir, "full")
        if backup_type in ("stream", "cloud"):
            os.makedirs(full_target, exist_ok=True)
            xb_cmd = self._xtrabackup_cmd_prefix() + [
                "--no-defaults", f"--user={self.backup_user}", "--password=",
                "--backup", f"--target-dir={full_target}_tmp",
                f"-S{self.socket_path}", f"--datadir={self.datadir}",
                "--stream=xbstream",
            ] + self.backup_params.split()

            log_file = os.path.join(self.logdir, f"full_backup_{log_date}_log")
            if backup_type == "cloud":
                cloud_name = f"full_{log_date}"
                pipe_cmd = f"{' '.join(xb_cmd)} 2>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbcloud')} put {cloud_params} {cloud_name} 2>>{log_file}"
                result = subprocess.run(pipe_cmd, shell=True, check=False)
                if result.returncode != 0:
                    pytest.fail(f"ERR: Full cloud backup failed. Please check the log at: {log_file}")
                get_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} get {cloud_params} {cloud_name} 2>>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbstream')} -xvC {full_target} 2>>{log_file}"
                result = subprocess.run(get_cmd, shell=True, check=False)
                if result.returncode != 0:
                    pytest.fail(f"ERR: Full cloud backup download failed. Please check the log at: {log_file}")
                del_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {cloud_name} 2>>{log_file}"
                subprocess.run(del_cmd, shell=True, check=False)
            else:
                stream_file = os.path.join(full_target, "full_backup.xbstream")
                with open(stream_file, "wb") as sf, open(log_file, "w") as lf:
                    proc = subprocess.run(xb_cmd, stdout=sf, stderr=lf, check=False)
                if proc.returncode != 0:
                    pytest.fail(f"ERR: Full Backup (stream) failed. Please check the log at: {log_file}")
                self.process_backup("stream", self.backup_params, full_target)
        elif backup_type == "tar":
            os.makedirs(full_target, exist_ok=True)
            xb_cmd = self._xtrabackup_cmd_prefix() + [
                "--no-defaults", f"--user={self.backup_user}", "--password=",
                "--backup", f"--target-dir={full_target}_tmp",
                f"-S{self.socket_path}", f"--datadir={self.datadir}",
                "--stream=tar",
            ] + self.backup_params.split()
            tar_file = os.path.join(full_target, "full_backup.tar")
            log_file = os.path.join(self.logdir, f"full_backup_{log_date}_log")
            with open(tar_file, "wb") as tf, open(log_file, "w") as lf:
                proc = subprocess.run(xb_cmd, stdout=tf, stderr=lf, check=False)
            if proc.returncode != 0:
                pytest.fail(f"ERR: Full Backup (tar) failed. Please check the log at: {log_file}")
            self.process_backup("tar", self.backup_params, full_target)
        else:
            cmd = self._xtrabackup_cmd_prefix() + [
                "--no-defaults", f"--user={self.backup_user}", "--password=",
                "--backup", f"--target-dir={full_target}",
                f"-S{self.socket_path}", f"--datadir={self.datadir}",
            ] + self.backup_params.split() + ["--register-redo-log-consumer"]

            log_file = os.path.join(self.logdir, f"full_backup_{log_date}_log")
            result = self.run_command(cmd, check=False, log_file=log_file)
            if result.returncode != 0:
                pytest.fail(f"ERR: Full Backup failed. Please check the log at: {log_file}")
            else:
                print(f"..Full backup was successfully created at: {full_target}.\n  Logs available at: {log_file}")
            self.process_backup("", self.backup_params, full_target)

        time.sleep(1)

        # --- Incremental backups ---
        inc_num = 1
        if single_incremental:
            if backup_type in ("stream", "cloud"):
                inc_target = os.path.join(self.backup_dir, "inc1")
                os.makedirs(inc_target, exist_ok=True)
                base_dir = full_target
                xb_cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults", f"--user={self.backup_user}", "--password=",
                    "--backup", f"--target-dir={inc_target}_tmp",
                    f"--incremental-basedir={base_dir}",
                    f"-S{self.socket_path}", f"--datadir={self.datadir}",
                    "--stream=xbstream",
                ] + self.backup_params.split()

                log_file = os.path.join(self.logdir, f"inc1_backup_{log_date}_log")
                if backup_type == "cloud":
                    cloud_name = f"inc1_{log_date}"
                    pipe_cmd = f"{' '.join(xb_cmd)} 2>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbcloud')} put {cloud_params} {cloud_name} 2>>{log_file}"
                    result = subprocess.run(pipe_cmd, shell=True, check=False)
                    if result.returncode != 0:
                        pytest.fail(f"ERR: Incremental cloud backup failed. Please check the log at: {log_file}")
                    get_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} get {cloud_params} {cloud_name} 2>>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbstream')} -xvC {inc_target} 2>>{log_file}"
                    result = subprocess.run(get_cmd, shell=True, check=False)
                    if result.returncode != 0:
                        pytest.fail(f"ERR: Inc cloud backup download failed. Please check the log at: {log_file}")
                    del_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {cloud_name} 2>>{log_file}"
                    subprocess.run(del_cmd, shell=True, check=False)
                else:
                    stream_file = os.path.join(inc_target, "inc_backup.xbstream")
                    with open(stream_file, "wb") as sf, open(log_file, "w") as lf:
                        proc = subprocess.run(xb_cmd, stdout=sf, stderr=lf, check=False)
                    if proc.returncode != 0:
                        pytest.fail(f"ERR: Incremental Backup (stream) failed. Please check the log at: {log_file}")
                    self.process_backup("stream", self.backup_params, inc_target)
            elif backup_type == "tar":
                pytest.fail("Incremental backup is not supported with tar streaming")
            else:
                print("=>Taking incremental backup: 1")
                base_dir = full_target
                inc_target = os.path.join(self.backup_dir, "inc1")
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults", f"--user={self.backup_user}", "--password=",
                    "--backup", f"--target-dir={inc_target}",
                    f"--incremental-basedir={base_dir}",
                    f"-S{self.socket_path}", f"--datadir={self.datadir}",
                ] + self.backup_params.split() + ["--register-redo-log-consumer"]

                log_file = os.path.join(self.logdir, f"inc1_backup_{log_date}_log")
                result = self.run_command(cmd, check=False, log_file=log_file)
                if result.returncode != 0:
                    with open(log_file, "r") as f:
                        log_content = f.read()
                    if "PXB will not be able to make a consistent backup" in log_content or "PXB will not be able to take a consistent backup" in log_content:
                        print("Retrying incremental backup with --lock-ddl option")
                        if os.path.exists(inc_target):
                            shutil.rmtree(inc_target)
                        cmd = self._xtrabackup_cmd_prefix() + [
                            "--no-defaults", f"--user={self.backup_user}", "--password=",
                            "--backup", f"--target-dir={inc_target}",
                            f"--incremental-basedir={base_dir}",
                            f"-S{self.socket_path}", f"--datadir={self.datadir}",
                        ] + self.backup_params.split() + [f"--lock-ddl={self.lock_ddl}", "--register-redo-log-consumer"]
                        result = self.run_command(cmd, check=False, log_file=log_file)
                        if result.returncode != 0:
                            pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
                    else:
                        pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
                else:
                    print(f"..Inc backup was successfully created at: {inc_target}.\n  Logs available at: {log_file}")
                self.process_backup("", self.backup_params, inc_target)
            inc_num = 2  # We took exactly one incremental (inc1)
        else:
            while self.is_load_running():
                print(f"=>Taking incremental backup: {inc_num}")
                base_dir = full_target if inc_num == 1 else os.path.join(self.backup_dir, f"inc{inc_num - 1}")
                inc_target = os.path.join(self.backup_dir, f"inc{inc_num}")

                if backup_type in ("stream", "cloud"):
                    os.makedirs(inc_target, exist_ok=True)
                    xb_cmd = self._xtrabackup_cmd_prefix() + [
                        "--no-defaults", f"--user={self.backup_user}", "--password=",
                        "--backup", f"--target-dir={inc_target}_tmp",
                        f"--incremental-basedir={base_dir}",
                        f"-S{self.socket_path}", f"--datadir={self.datadir}",
                        "--stream=xbstream",
                    ] + self.backup_params.split()

                    log_file = os.path.join(self.logdir, f"inc{inc_num}_backup_{log_date}_log")
                    if backup_type == "cloud":
                        cloud_name = f"inc{inc_num}_{log_date}"
                        pipe_cmd = f"{' '.join(xb_cmd)} 2>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbcloud')} put {cloud_params} {cloud_name} 2>>{log_file}"
                        result = subprocess.run(pipe_cmd, shell=True, check=False)
                        if result.returncode != 0:
                            pytest.fail(f"ERR: Inc{inc_num} cloud backup failed. Log: {log_file}")
                        get_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} get {cloud_params} {cloud_name} 2>>{log_file} | {os.path.join(self.xtrabackup_dir, 'xbstream')} -xvC {inc_target} 2>>{log_file}"
                        result = subprocess.run(get_cmd, shell=True, check=False)
                        if result.returncode != 0:
                            pytest.fail(f"ERR: Inc{inc_num} cloud download failed. Log: {log_file}")
                        del_cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {cloud_name} 2>>{log_file}"
                        subprocess.run(del_cmd, shell=True, check=False)
                    else:
                        stream_file = os.path.join(inc_target, "inc_backup.xbstream")
                        with open(stream_file, "wb") as sf, open(log_file, "w") as lf:
                            proc = subprocess.run(xb_cmd, stdout=sf, stderr=lf, check=False)
                        if proc.returncode != 0:
                            pytest.fail(f"ERR: Inc{inc_num} stream backup failed. Log: {log_file}")
                        self.process_backup("stream", self.backup_params, inc_target)
                else:
                    cmd = self._xtrabackup_cmd_prefix() + [
                        "--no-defaults", f"--user={self.backup_user}", "--password=",
                        "--backup", f"--target-dir={inc_target}",
                        f"--incremental-basedir={base_dir}",
                        f"-S{self.socket_path}", f"--datadir={self.datadir}",
                    ] + self.backup_params.split() + ["--register-redo-log-consumer"]

                    log_file = os.path.join(self.logdir, f"inc{inc_num}_backup_{log_date}_log")
                    result = self.run_command(cmd, check=False, log_file=log_file)

                    if result.returncode != 0:
                        with open(log_file, "r") as f:
                            log_content = f.read()
                        if "PXB will not be able to make a consistent backup" in log_content or "PXB will not be able to take a consistent backup" in log_content:
                            print("Retrying incremental backup with --lock-ddl option")
                            if os.path.exists(inc_target):
                                shutil.rmtree(inc_target)
                            cmd = self._xtrabackup_cmd_prefix() + [
                                "--no-defaults", f"--user={self.backup_user}", "--password=",
                                "--backup", f"--target-dir={inc_target}",
                                f"--incremental-basedir={base_dir}",
                                f"-S{self.socket_path}", f"--datadir={self.datadir}",
                            ] + self.backup_params.split() + [f"--lock-ddl={self.lock_ddl}", "--register-redo-log-consumer"]
                            result = self.run_command(cmd, check=False, log_file=log_file)
                            if result.returncode != 0:
                                pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
                        else:
                            pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
                    else:
                        print(f"..Inc backup was successfully created at: {inc_target}.\n  Logs available at: {log_file}")
                    self.process_backup("", self.backup_params, inc_target)

                inc_num += 1
                time.sleep(10)

        # Stop any remaining DDL threads (best effort)
        for t in threading.enumerate():
            if t.name.startswith("ddl_"):
                t.join(timeout=5)

        # --- Prepare backups ---
        print("=>Preparing full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults", "--prepare", "--apply-log-only",
            f"--target_dir={full_target}",
        ] + self.prepare_params.split()

        log_file = os.path.join(self.logdir, f"prepare_full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Prepare of full backup failed. Please check the log at: {log_file}")
        else:
            print(f"..Prepare of full backup was successful.\n  Logs available at: {log_file}")

        total_inc = inc_num - 1
        for i in range(1, total_inc + 1):
            print(f"=>Preparing incremental backup: {i}")
            if i == total_inc:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults", "--prepare",
                    f"--target_dir={full_target}",
                    f"--incremental-dir={self.backup_dir}/inc{i}",
                ] + self.prepare_params.split()
            else:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults", "--prepare", "--apply-log-only",
                    f"--target_dir={full_target}",
                    f"--incremental-dir={self.backup_dir}/inc{i}",
                ] + self.prepare_params.split()

            log_file = os.path.join(self.logdir, f"prepare_inc{i}_backup_{log_date}_log")
            result = self.run_command(cmd, check=False, log_file=log_file)
            if result.returncode != 0:
                pytest.fail(f"ERR: Prepare of incremental backup failed. Please check the log at: {log_file}")
            else:
                print(f"..Prepare of incremental backup was successful.\n  Logs available at: {log_file}")

        # --- Collect data before restore ---
        if use_detailed_verify:
            print("Collecting table data before restore (detailed verify)")
            orig_data = self.collect_table_data(databases)
        else:
            print("Collecting existing table count")
            old_cwd = os.getcwd()
            os.chdir(self.logdir)
            try:
                with open("file1", "w") as f:
                    result = subprocess.run(
                        ["pt-table-checksum", f"S={self.socket_path},u=root",
                         "-d", "test", "--recursion-method", "none", "--no-check-binlog-format"],
                        stdout=f, check=False,
                    )
                    if result.returncode not in (0, 64):
                        raise subprocess.CalledProcessError(result.returncode, result.args)
                with open("file1", "r") as f:
                    lines = f.readlines()
                with open("file1", "w") as f:
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 9:
                            f.write(f"{parts[3]} {parts[8]}\n")
            finally:
                os.chdir(old_cwd)

        time.sleep(2)

        # --- Stop server and move data directory ---
        print("Stopping mysql server and moving data directory")
        subprocess.run(
            [os.path.join(self.mysqldir, "bin/mysqladmin"), "-uroot", f"-S{self.socket_path}", "shutdown"],
            check=True,
        )

        data_orig = os.path.join(self.backup_dir, f"data_orig_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        if os.path.exists(data_orig):
            shutil.rmtree(data_orig)
        shutil.move(self.datadir, data_orig)

        # --- Restore backup ---
        print("=>Restoring full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults", "--copy-back",
            f"--target-dir={full_target}",
            f"--datadir={self.datadir}",
        ] + self.restore_params.split()

        log_file = os.path.join(self.logdir, f"res_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Restore of full backup failed. Please check the log at: {log_file}")
        else:
            print(f"..Restore of full backup was successful.\n  Logs available at: {log_file}")

        # Copy .pem files from original datadir for SSL
        for pem_file in glob.glob(os.path.join(data_orig, "*.pem")):
            shutil.copy2(pem_file, self.datadir)

        # Copy keyring file if it exists as transition-key
        keyring_in_backup = os.path.join(full_target, "keyring")
        if os.path.isfile(keyring_in_backup):
            shutil.copy2(keyring_in_backup, self.logdir)

        self.start_server()

        # --- Verify ---
        if use_detailed_verify:
            self.verify_data_integrity(databases, orig_data)
        else:
            if (
                "binlog-encryption" not in self.mysqld_options
                and "encrypt-binlog" not in self.mysqld_options
                and "skip-log-bin" not in self.mysqld_options
            ):
                print("Check xtrabackup for binlog position")
                binlog_info_file = os.path.join(full_target, "xtrabackup_binlog_info")
                with open(binlog_info_file, "r") as f:
                    line = f.readline().strip()
                    parts = line.split()
                    xb_binlog_file = parts[0] if parts else ""
                    xb_binlog_pos = parts[1] if len(parts) > 1 else ""

                print(f"Xtrabackup binlog position: {xb_binlog_file}, {xb_binlog_pos}")
                print(f"Applying binlog to restored data starting from {xb_binlog_file}, {xb_binlog_pos}")
                binlog_path = os.path.join(data_orig, xb_binlog_file)
                if os.path.exists(binlog_path):
                    mysqlbinlog = subprocess.Popen(
                        [os.path.join(self.mysqldir, "bin/mysqlbinlog"), binlog_path, f"--start-position={xb_binlog_pos}"],
                        stdout=subprocess.PIPE,
                    )
                    mysql = subprocess.Popen(
                        [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}"],
                        stdin=mysqlbinlog.stdout,
                    )
                    mysqlbinlog.stdout.close()
                    mysql.communicate()
                    if mysql.returncode != 0:
                        print("ERR: The binlog could not be applied to the restored data")

                time.sleep(5)

                print("Collecting table count after restore")
                old_cwd = os.getcwd()
                os.chdir(self.logdir)
                try:
                    with open("file2", "w") as f:
                        result = subprocess.run(
                            ["pt-table-checksum", f"S={self.socket_path},u=root",
                             "-d", "test", "--recursion-method", "none", "--no-check-binlog-format"],
                            stdout=f, check=False,
                        )
                        if result.returncode not in (0, 64):
                            raise subprocess.CalledProcessError(result.returncode, result.args)
                    with open("file2", "r") as f:
                        lines = f.readlines()
                    with open("file2", "w") as f:
                        for line in lines:
                            parts = line.split()
                            if len(parts) >= 9:
                                f.write(f"{parts[3]} {parts[8]}\n")
                    result = subprocess.run(["diff", "file1", "file2"], capture_output=True, text=True)
                    if result.returncode != 0:
                        print("ERR: Difference found in table count before and after restore.")
                    else:
                        print("Data is the same before and after restore: Pass")
                        os.remove("file1")
                        os.remove("file2")
                finally:
                    os.chdir(old_cwd)
            else:
                print("Binlog applying skipped, ignore differences between actual data and restored data")

    def process_backup(self, backup_type: str, backup_params: str, target_dir: str):
        """Post-backup processing: extract xbstream/tar, decrypt, decompress."""
        if backup_type == "stream":
            stream_file = os.path.join(target_dir, "full_backup.xbstream")
            if not os.path.isfile(stream_file):
                stream_file = os.path.join(target_dir, "inc_backup.xbstream")
            if os.path.isfile(stream_file):
                print(f"Extracting xbstream from {stream_file}")
                with open(stream_file, "rb") as sf:
                    subprocess.run(
                        [os.path.join(self.xtrabackup_dir, "xbstream"), "-xvC", target_dir],
                        stdin=sf, capture_output=True, check=True,
                    )
                os.remove(stream_file)
        elif backup_type == "tar":
            tar_file = os.path.join(target_dir, "full_backup.tar")
            if os.path.isfile(tar_file):
                print(f"Extracting tar from {tar_file}")
                subprocess.run(["tar", "-xvf", tar_file, "-C", target_dir], capture_output=True, check=True)
                os.remove(tar_file)

        if "--encrypt=" in backup_params:
            print("Decrypting backup files")
            xb_cmd = self._xtrabackup_cmd_prefix() + [
                "--decrypt=AES256", f"--encrypt-key={self.encrypt_key}", f"--target-dir={target_dir}",
            ]
            subprocess.run(xb_cmd, capture_output=True, check=True)
            for enc_file in glob.glob(os.path.join(target_dir, "**/*.xbcrypt"), recursive=True):
                os.remove(enc_file)

        if "--compress" in backup_params:
            print("Decompressing backup files")
            xb_cmd = self._xtrabackup_cmd_prefix() + ["--decompress", f"--target-dir={target_dir}"]
            subprocess.run(xb_cmd, capture_output=True, check=True)
            for qp_file in glob.glob(os.path.join(target_dir, "**/*.qp"), recursive=True):
                os.remove(qp_file)
            for lz4_file in glob.glob(os.path.join(target_dir, "**/*.lz4"), recursive=True):
                os.remove(lz4_file)
            for zst_file in glob.glob(os.path.join(target_dir, "**/*.zst"), recursive=True):
                os.remove(zst_file)

    def collect_table_data(self, databases: List[str]) -> Dict[str, Dict[str, Tuple[str, str]]]:
        """Collect per-table COUNT(*) and CHECKSUM for verification.

        Returns dict: {db: {table: (count, checksum)}}
        """
        data: Dict[str, Dict[str, Tuple[str, str]]] = {}
        for db in databases:
            data[db] = {}
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                 f"SHOW TABLES FROM {db};"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                continue
            tables = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
            for table in tables:
                count_result = subprocess.run(
                    [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                     f"SELECT COUNT(*) FROM {db}.{table}"],
                    capture_output=True, text=True, check=False,
                )
                count = count_result.stdout.strip() if count_result.returncode == 0 else "ERR"
                cksum_result = subprocess.run(
                    [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                     f"CHECKSUM TABLE {db}.{table}"],
                    capture_output=True, text=True, check=False,
                )
                cksum_parts = cksum_result.stdout.strip().split() if cksum_result.returncode == 0 else []
                cksum = cksum_parts[1] if len(cksum_parts) >= 2 else "ERR"
                data[db][table] = (count, cksum)
        return data

    def verify_data_integrity(self, databases: List[str], orig_data: Optional[Dict] = None):
        """Per-table CHECK TABLE, record count comparison, checksum comparison, and ID gap detection."""
        check_err = 0
        for db in databases:
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                 f"SHOW TABLES FROM {db};"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                print(f"Warning: Could not list tables in {db}")
                continue
            tables = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
            for table in tables:
                check_result = subprocess.run(
                    [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                     f"CHECK TABLE {db}.{table}"],
                    capture_output=True, text=True, check=False,
                )
                if check_result.returncode != 0 or "OK" not in check_result.stdout:
                    print(f"ERR: CHECK TABLE {db}.{table} failed: {check_result.stdout}")
                    check_err = 1

                if orig_data and db in orig_data and table in orig_data[db]:
                    orig_count, orig_cksum = orig_data[db][table]
                    count_result = subprocess.run(
                        [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                         f"SELECT COUNT(*) FROM {db}.{table}"],
                        capture_output=True, text=True, check=False,
                    )
                    new_count = count_result.stdout.strip() if count_result.returncode == 0 else "ERR"
                    if new_count != orig_count:
                        print(f"Warning: Row count mismatch for {db}.{table}: before={orig_count}, after={new_count}")

                    cksum_result = subprocess.run(
                        [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-BNe",
                         f"CHECKSUM TABLE {db}.{table}"],
                        capture_output=True, text=True, check=False,
                    )
                    cksum_parts = cksum_result.stdout.strip().split() if cksum_result.returncode == 0 else []
                    new_cksum = cksum_parts[1] if len(cksum_parts) >= 2 else "ERR"
                    if new_cksum != orig_cksum:
                        print(f"Warning: Checksum mismatch for {db}.{table}: before={orig_cksum}, after={new_cksum}")

        ping_result = subprocess.run(
            [os.path.join(self.mysqldir, "bin/mysqladmin"), "ping", "--user=root", f"--socket={self.socket_path}"],
            capture_output=True, check=False,
        )
        if ping_result.returncode != 0:
            pytest.fail("ERR: The database has gone down due to corruption, the restore was unsuccessful")

        if check_err == 0:
            print("All table status: OK")
        else:
            print("After restore, some tables may be corrupt, check table status is not OK")

    def stop_vault_server(self) -> None:
        """Kill any running HashiCorp Vault server process."""
        result = subprocess.run(
            ["pgrep", "-f", "vault server"],
            capture_output=True, text=True, check=False,
        )
        pids = result.stdout.strip().split()
        if pids:
            print(f"Stopping vault server (PIDs: {', '.join(pids)})...")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, OSError, ValueError):
                    pass
            for _ in range(10):
                time.sleep(1)
                check = subprocess.run(
                    ["pgrep", "-f", "vault server"],
                    capture_output=True, text=True, check=False,
                )
                if not check.stdout.strip():
                    break
            else:
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError, ValueError):
                        pass

    def start_vault_server(self) -> Dict[str, str]:
        """Start Vault server using vault_test_setup.sh and return vault config dict."""
        self.stop_vault_server()
        vault_setup = os.path.join(self.qascripts, "vault_test_setup.sh")
        if not os.path.isfile(vault_setup):
            pytest.fail(f"vault_test_setup.sh not found at {vault_setup}")

        vault_dir = os.path.join(HOME, "vault")
        log_file = os.path.join(self.logdir, "vault_setup.log")
        result = subprocess.run(
            ["bash", vault_setup, f"--workdir={vault_dir}", "--use-ssl"],
            capture_output=True, text=True, check=False,
        )
        with open(log_file, "w") as f:
            f.write(result.stdout + "\n" + result.stderr)
        if result.returncode != 0:
            pytest.fail(f"Vault setup failed. Log: {log_file}")

        vault_config: Dict[str, str] = {}
        cnf_file = os.path.join(vault_dir, "keyring_vault_ps.cnf")
        if os.path.isfile(cnf_file):
            with open(cnf_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, val = line.split("=", 1)
                        vault_config[key.strip()] = val.strip()
        vault_config["cnf_file"] = cnf_file
        vault_config["vault_dir"] = vault_dir
        return vault_config

    def create_keyring_manifest(self, component_name: str):
        """Create mysqld.my manifest file for component-based keyrings."""
        manifest_file = os.path.join(self.mysqldir, "bin/mysqld.my")
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write(f'{{\n  "components": "file://{component_name}"\n}}\n')
        return manifest_file

    def create_keyring_config(self, encrypt_type: str, **kwargs) -> str:
        """Create component configuration file for the given encryption type.

        Returns path to the created config file.
        """
        plugin_dir = os.path.join(self.mysqldir, "lib/plugin")
        os.makedirs(plugin_dir, exist_ok=True)

        if encrypt_type in ("keyring_file_component", "keyring_file"):
            config_file = os.path.join(plugin_dir, "component_keyring_file.cnf")
            keyring_path = kwargs.get("keyring_path", os.path.join(self.logdir, "keyring"))
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(f'{{\n  "path": "{keyring_path}",\n  "read_only": false\n}}\n')
        elif encrypt_type in ("keyring_vault_component", "keyring_vault"):
            config_file = os.path.join(plugin_dir, "component_keyring_vault.cnf")
            vault_config = kwargs.get("vault_config", {})
            vault_url = vault_config.get("vault_url", "")
            secret_mount_point = vault_config.get("secret_mount_point", "")
            token = vault_config.get("token", "")
            vault_ca = vault_config.get("vault_ca", "")
            with open(config_file, "w", encoding="utf-8") as f:
                content = (
                    f'{{\n  "vault_url": "{vault_url}",\n'
                    f'  "secret_mount_point": "{secret_mount_point}",\n'
                    f'  "token": "{token}",\n'
                    f'  "vault_ca": "{vault_ca}"\n}}\n'
                )
                f.write(content)
        elif encrypt_type in ("keyring_kmip_component", "keyring_kmip"):
            cert_dir = kwargs.get("cert_dir", "")
            config_file = os.path.join(plugin_dir, "component_keyring_kmip.cnf")
            src = os.path.join(cert_dir, "component_keyring_kmip.cnf")
            if os.path.isfile(src):
                shutil.copy2(src, config_file)
        elif encrypt_type in ("keyring_kms_component", "keyring_kms"):
            config_file = os.path.join(plugin_dir, "component_keyring_kms.cnf")
            keyring_path = kwargs.get("keyring_path", os.path.join(self.logdir, "keyring_kms"))
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(
                    f'{{\n  "path": "{keyring_path}", "region": "{self.kms_region}", '
                    f'"kms_key": "{self.kms_id}", "auth_key": "{self.kms_auth_key}", '
                    f'"secret_access_key": "{self.kms_secret_key}", "read_only": false\n}}\n'
                )
        else:
            pytest.fail(f"Unknown encrypt_type: {encrypt_type}")
            return ""
        return config_file

    def cleanup_keyring_configs(self):
        """Remove manifest and config files."""
        manifest = os.path.join(self.mysqldir, "bin/mysqld.my")
        if os.path.isfile(manifest):
            os.remove(manifest)
        plugin_dir = os.path.join(self.mysqldir, "lib/plugin")
        for cnf in ["component_keyring_file.cnf", "component_keyring_vault.cnf",
                     "component_keyring_kmip.cnf", "component_keyring_kms.cnf"]:
            path = os.path.join(plugin_dir, cnf)
            if os.path.isfile(path):
                os.remove(path)

    def xbcloud_put(self, cloud_params: str, name: str, stream_file: str):
        """Upload backup stream to cloud."""
        cmd = f"cat {stream_file} | {os.path.join(self.xtrabackup_dir, 'xbcloud')} put {cloud_params} {name}"
        log_file = os.path.join(self.logdir, f"xbcloud_put_{name}.log")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
        with open(log_file, "w") as f:
            f.write(result.stdout + "\n" + result.stderr)
        if result.returncode != 0:
            pytest.fail(f"xbcloud put failed for {name}. Log: {log_file}")

    def xbcloud_get(self, cloud_params: str, name: str, target_dir: str):
        """Download backup from cloud."""
        os.makedirs(target_dir, exist_ok=True)
        cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} get {cloud_params} {name} | {os.path.join(self.xtrabackup_dir, 'xbstream')} -xvC {target_dir}"
        log_file = os.path.join(self.logdir, f"xbcloud_get_{name}.log")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
        with open(log_file, "w") as f:
            f.write(result.stdout + "\n" + result.stderr)
        if result.returncode != 0:
            pytest.fail(f"xbcloud get failed for {name}. Log: {log_file}")

    def xbcloud_delete(self, cloud_params: str, name: str):
        """Delete backup from cloud."""
        cmd = f"{os.path.join(self.xtrabackup_dir, 'xbcloud')} delete {cloud_params} {name}"
        subprocess.run(cmd, shell=True, capture_output=True, check=False)

    def run_ddl_in_background(self, ddl_func, *args, **kwargs) -> threading.Thread:
        """Launch a DDL operation in a background thread. Returns the thread handle."""
        thread = threading.Thread(target=ddl_func, args=args, kwargs=kwargs, daemon=True, name=f"ddl_{ddl_func.__name__}")
        thread.start()
        return thread

    def _is_server_alive(self) -> bool:
        """Check if the primary MySQL server is alive via ``mysqladmin ping``."""
        return self.primary.is_alive()

    def _run_sql(self, sql: str, check: bool = False) -> bool:
        """Run SQL statement; returns True on success."""
        result = subprocess.run(
            [os.path.join(self.mysqldir, "bin/mysql"), "-uroot", f"-S{self.socket_path}", "-e", sql],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0

    # --- DDL operation methods (each runs in background via run_ddl_in_background) ---

    def ddl_change_storage_engine(self):
        """Change storage engine of tables between MYISAM and INNODB (and ROCKSDB if enabled)."""
        print("DDL: Change storage engine of test.sbtest1")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 ENGINE=MYISAM;")
            self._run_sql("ALTER TABLE test.sbtest1 ENGINE=INNODB;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ENGINE=INNODB;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ENGINE=ROCKSDB;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ENGINE=MYISAM;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ENGINE=ROCKSDB;")

    def ddl_add_drop_index(self):
        """Add and drop indexes on tables."""
        print("DDL: Add and drop index on test.sbtest1")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE INDEX kc ON test.sbtest1 (k,c);")
            self._run_sql("ALTER TABLE test.sbtest1 ADD INDEX kc2 (k,c);")
            self._run_sql("DROP INDEX kc2 ON test.sbtest1;")
            self._run_sql("DROP INDEX kc ON test.sbtest1;")
            self._run_sql("ALTER TABLE test.sbtest1 ADD INDEX kc (k,c), ALGORITHM=COPY, LOCK=EXCLUSIVE;")
            self._run_sql("DROP INDEX kc ON test.sbtest1;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("CREATE INDEX kc ON test_rocksdb.sbtest1 (k,c);")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ADD INDEX kc2 (k,c);")
                self._run_sql("DROP INDEX kc2 ON test_rocksdb.sbtest1;")
                self._run_sql("DROP INDEX kc ON test_rocksdb.sbtest1;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 ADD INDEX kc (k,c), ALGORITHM=COPY, LOCK=EXCLUSIVE;")
                self._run_sql("DROP INDEX kc ON test_rocksdb.sbtest1;")

    def ddl_rename_index(self):
        """Rename indexes on tables."""
        print("DDL: Rename index on test.sbtest1")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 RENAME INDEX k_1 TO k_2, ALGORITHM=INPLACE, LOCK=NONE;")
            self._run_sql("ALTER TABLE test.sbtest1 RENAME INDEX k_2 TO k_1, ALGORITHM=INPLACE, LOCK=NONE;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 RENAME INDEX k_1 TO k_2, ALGORITHM=INPLACE, LOCK=NONE;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 RENAME INDEX k_2 TO k_1, ALGORITHM=INPLACE, LOCK=NONE;")

    def ddl_add_drop_full_text_index(self):
        """Add and drop full text index."""
        print("DDL: Add and drop full text index on test.sbtest1")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE FULLTEXT INDEX full_index ON test.sbtest1 (pad);")
            self._run_sql("DROP INDEX full_index ON test.sbtest1;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("CREATE FULLTEXT INDEX full_index ON test_rocksdb.sbtest1 (pad);")
                self._run_sql("DROP INDEX full_index ON test_rocksdb.sbtest1;")

    def ddl_change_index_type(self):
        """Change index type between BTREE and HASH."""
        print("DDL: Change index type on test.sbtest1")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 DROP INDEX k_1, ADD INDEX k_1(k) USING BTREE, ALGORITHM=INSTANT;")
            self._run_sql("ALTER TABLE test.sbtest1 DROP INDEX k_1, ADD INDEX k_1(k) USING HASH, ALGORITHM=INSTANT;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 DROP INDEX k_1, ADD INDEX k_1(k) USING BTREE, ALGORITHM=INSTANT;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 DROP INDEX k_1, ADD INDEX k_1(k) USING HASH, ALGORITHM=INSTANT;")

    def ddl_add_drop_spatial_index(self):
        """Add data to spatial table and add/drop spatial index."""
        print("DDL: Spatial data and index on test.geom")
        a, b = 1, 2
        for _ in range(100):
            if not self._is_server_alive():
                break
            self._run_sql(f"INSERT INTO test.geom VALUES(POINT({a},{b}));")
            a += 1
            b += 1
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("CREATE SPATIAL INDEX spa_index ON test.geom (g), ALGORITHM=INPLACE, LOCK=SHARED;")
                self._run_sql("DROP INDEX spa_index ON test.geom;")

    def ddl_add_drop_tablespace(self):
        """Add table to tablespace and drop both."""
        print("DDL: Add and drop tablespace")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE TABLESPACE ts1 ADD DATAFILE 'ts1.ibd' Engine=InnoDB;")
            self._run_sql("CREATE TABLE test.sbtest1copy SELECT * FROM test.sbtest1;")
            self._run_sql("ALTER TABLE test.sbtest1copy TABLESPACE ts1;")
            self._run_sql("DROP TABLE test.sbtest1copy;")
            self._run_sql("DROP TABLESPACE ts1;")
        if self.rocksdb == "enabled":
            for i in range(1, 11):
                if not self._is_server_alive():
                    break
                self._run_sql(f"CREATE TABLE test_rocksdb.sbrcopy{i} Engine=ROCKSDB SELECT * FROM test.sbtest1;")
                self._run_sql(f"DROP TABLE test_rocksdb.sbrcopy{i};")

    def ddl_change_compression(self):
        """Change compression of tables."""
        print("DDL: Change compression")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 COMPRESSION='lz4';")
            self._run_sql("ALTER TABLE test.sbtest1 COMPRESSION='zlib';")
            self._run_sql("ALTER TABLE test.sbtest1 COMPRESSION='';")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 COMMENT = 'cfname=cf1';")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 COMMENT = 'cfname=cf2';")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 COMMENT = 'cfname=cf3';")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest1 COMMENT = 'cfname=cf4';")

    def ddl_change_row_format(self):
        """Change row format of tables."""
        print("DDL: Change row format")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest2 ROW_FORMAT=COMPRESSED;")
            self._run_sql("ALTER TABLE test.sbtest2 ROW_FORMAT=DYNAMIC;")
            self._run_sql("ALTER TABLE test.sbtest2 ROW_FORMAT=COMPACT;")
            self._run_sql("ALTER TABLE test.sbtest2 ROW_FORMAT=REDUNDANT;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("ALTER TABLE test_rocksdb.sbtest2 ROW_FORMAT=COMPRESSED;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest2 ROW_FORMAT=DYNAMIC;")
                self._run_sql("ALTER TABLE test_rocksdb.sbtest2 ROW_FORMAT=FIXED;")

    def ddl_add_data_transaction(self):
        """Add data in both innodb and myrocks tables in a single transaction."""
        print("DDL: Cross-engine transaction inserts")
        self._run_sql("CREATE TABLE IF NOT EXISTS test.innodb_t(id int(11) PRIMARY KEY AUTO_INCREMENT, k int(11), c char(120), pad char(60), KEY k_1(k), KEY kc(k,c)) ENGINE=InnoDB;")
        self._run_sql("CREATE TABLE IF NOT EXISTS test.myrocks_t(id int(11) PRIMARY KEY AUTO_INCREMENT, k int(11), c char(120), pad char(60), KEY k_1(k), KEY kc(k,c)) ENGINE=ROCKSDB;")
        a, b, c = 1, 11, 101
        for _ in range(200):
            if not self._is_server_alive():
                break
            self._run_sql(f"START TRANSACTION; INSERT INTO test.innodb_t(k, c, pad) VALUES({a}, {b}, {c}); INSERT INTO test.myrocks_t(k, c, pad) VALUES({a}, {b}, {c}); COMMIT;")
            a += 1
            b += 1
            c += 1

    def ddl_update_truncate_table(self):
        """Update and truncate tables."""
        print("DDL: Update and truncate tables")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("UPDATE test.sbtest1 SET c='test_update_data';")
            self._run_sql("OPTIMIZE TABLE test.sbtest1;")
            self._run_sql("TRUNCATE test.sbtest1;")
        if self.rocksdb == "enabled":
            for _ in range(10):
                if not self._is_server_alive():
                    break
                self._run_sql("UPDATE test_rocksdb.sbtest2 SET c='test_update_data';")
                self._run_sql("OPTIMIZE TABLE test_rocksdb.sbtest2;")
                self._run_sql("TRUNCATE test_rocksdb.sbtest2;")

    def ddl_create_drop_database(self):
        """Create a database, add data, and drop it."""
        print("DDL: Create and drop database")
        for _ in range(3):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE DATABASE IF NOT EXISTS test1_innodb;")
            subprocess.run(
                ["sysbench", "/usr/share/sysbench/oltp_insert.lua", "--tables=1", "--table-size=1000",
                 "--mysql-db=test1_innodb", "--mysql-user=root", "--threads=10", "--db-driver=mysql",
                 f"--mysql-socket={self.socket_path}", "prepare"],
                capture_output=True, check=False,
            )
            self._run_sql("ALTER TABLE test1_innodb.sbtest1 ADD COLUMN b JSON AS('{\"k1\": \"value\", \"k2\": [10, 20]}');")
            self._run_sql("CREATE INDEX jindex ON test1_innodb.sbtest1( (CAST(b->'$.k2' AS UNSIGNED ARRAY)) );")
            self._run_sql("DROP INDEX jindex ON test1_innodb.sbtest1;")
            self._run_sql("ALTER TABLE test1_innodb.sbtest1 DROP COLUMN b;")
            self._run_sql("DROP DATABASE test1_innodb;")
        if self.rocksdb == "enabled":
            for _ in range(3):
                if not self._is_server_alive():
                    break
                self._run_sql("CREATE DATABASE IF NOT EXISTS test1_rocksdb;")
                subprocess.run(
                    ["sysbench", "/usr/share/sysbench/oltp_insert.lua", "--tables=1", "--table-size=1000",
                     "--mysql-db=test1_rocksdb", "--mysql-user=root", "--threads=10", "--db-driver=mysql",
                     "--mysql-storage-engine=ROCKSDB", f"--mysql-socket={self.socket_path}", "prepare"],
                    capture_output=True, check=False,
                )
                self._run_sql("ALTER TABLE test1_rocksdb.sbtest1 ADD COLUMN b VARCHAR(255) DEFAULT '{\"k1\": \"value\", \"k2\": [10, 20]}';")
                self._run_sql("ALTER TABLE test1_rocksdb.sbtest1 DROP COLUMN b;")
                self._run_sql("DROP DATABASE test1_rocksdb;")

    def ddl_create_delete_encrypted_table(self):
        """Create an encrypted table, add data, and delete it."""
        print("DDL: Create and delete encrypted tables")
        self._run_sql("CREATE DATABASE IF NOT EXISTS test_innodb;")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE TABLE test_innodb.sbtest1 (id int(11) NOT NULL AUTO_INCREMENT, k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y' COMPRESSION='lz4';")
            subprocess.run(
                ["sysbench", "/usr/share/sysbench/oltp_insert.lua", "--tables=1",
                 "--mysql-db=test_innodb", "--mysql-user=root", "--threads=100", "--db-driver=mysql",
                 f"--mysql-socket={self.socket_path}", "--time=1", "run"],
                capture_output=True, check=False,
            )
            self._run_sql("DROP TABLE test_innodb.sbtest1;")

    def ddl_change_encryption(self):
        """Toggle encryption on a table."""
        print("DDL: Change encryption")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 ENCRYPTION='N';")
            self._run_sql("ALTER TABLE test.sbtest1 ENCRYPTION='Y';")

    def ddl_compressed_column(self):
        """Compress and uncompress a column."""
        print("DDL: Compressed column")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 MODIFY c VARCHAR(250) COLUMN_FORMAT COMPRESSED NOT NULL DEFAULT '';")
            self._run_sql("ALTER TABLE test.sbtest1 MODIFY c CHAR(120) COLUMN_FORMAT DEFAULT NOT NULL DEFAULT '';")

    def ddl_compression_dictionary(self):
        """Use compression dictionary to compress columns."""
        print("DDL: Compression dictionary")
        if not self._run_sql("CREATE COMPRESSION_DICTIONARY numbers('08566691963-88624912351-16662227201-46648573979-64646226163-77505759394-75470094713-41097360717-15161106334-50535565977');"):
            print("Compression dictionary not supported, skipping")
            return
        for i in range(1, self.num_tables + 1):
            if not self._is_server_alive():
                break
            self._run_sql(f"ALTER TABLE test.sbtest{i} MODIFY c VARCHAR(250) COLUMN_FORMAT COMPRESSED WITH COMPRESSION_DICTIONARY numbers NOT NULL DEFAULT '';")
            self._run_sql(f"ALTER TABLE test.sbtest{i} MODIFY c CHAR(120) COLUMN_FORMAT DEFAULT NOT NULL DEFAULT '';")

    def ddl_partitioned_tables(self):
        """Create and manage partitioned tables."""
        print("DDL: Partitioned tables")
        self._run_sql("DROP TABLE IF EXISTS test.sbtest1; DROP TABLE IF EXISTS test.sbtest2; DROP TABLE IF EXISTS test.sbtest3;")
        self._run_sql("CREATE TABLE test.sbtest1 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) PARTITION BY HASH(id) PARTITIONS 10;")
        self._run_sql("CREATE TABLE test.sbtest2 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) PARTITION BY RANGE(id) (PARTITION p0 VALUES LESS THAN (500), PARTITION p1 VALUES LESS THAN (1000), PARTITION p2 VALUES LESS THAN MAXVALUE);")
        self._run_sql("CREATE TABLE test.sbtest3 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) PARTITION BY KEY() PARTITIONS 5;")
        subprocess.run(
            ["sysbench", "/usr/share/sysbench/oltp_insert.lua", "--tables=3", "--mysql-db=test",
             "--mysql-user=root", "--threads=100", "--db-driver=mysql",
             f"--mysql-socket={self.socket_path}", "--time=5", "run"],
            capture_output=True, check=False,
        )
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 COALESCE PARTITION 5;")
            self._run_sql("ALTER TABLE test.sbtest1 PARTITION BY HASH(id) PARTITIONS 10;")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest2 DROP PARTITION p2;")
            self._run_sql("ALTER TABLE test.sbtest2 ADD PARTITION (PARTITION p2 VALUES LESS THAN MAXVALUE);")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest3 REBUILD PARTITION p0, p1;")
            self._run_sql("ALTER TABLE test.sbtest3 OPTIMIZE PARTITION p2;")
            self._run_sql("ALTER TABLE test.sbtest3 ANALYZE PARTITION p3,p4;")
        if self.rocksdb == "enabled":
            self._run_sql("DROP TABLE IF EXISTS test_rocksdb.sbtest1; DROP TABLE IF EXISTS test_rocksdb.sbtest2; DROP TABLE IF EXISTS test_rocksdb.sbtest3;")
            self._run_sql("CREATE TABLE test_rocksdb.sbtest1 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) ENGINE=ROCKSDB PARTITION BY HASH(id) PARTITIONS 10;")
            self._run_sql("CREATE TABLE test_rocksdb.sbtest2 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) ENGINE=ROCKSDB PARTITION BY RANGE(id) (PARTITION p0 VALUES LESS THAN (500), PARTITION p1 VALUES LESS THAN (1000), PARTITION p2 VALUES LESS THAN MAXVALUE);")
            self._run_sql("CREATE TABLE test_rocksdb.sbtest3 (id int NOT NULL AUTO_INCREMENT, k int NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k) ) ENGINE=ROCKSDB PARTITION BY KEY() PARTITIONS 5;")
            subprocess.run(
                ["sysbench", "/usr/share/sysbench/oltp_insert.lua", "--tables=3", "--mysql-db=test_rocksdb",
                 "--mysql-user=root", "--threads=100", "--db-driver=mysql",
                 "--mysql-storage-engine=ROCKSDB", f"--mysql-socket={self.socket_path}", "--time=5", "run"],
                capture_output=True, check=False,
            )

    def ddl_grant_tables(self):
        """Create user, grant privileges, then drop."""
        print("DDL: Grant tables")
        for _ in range(50):
            if not self._is_server_alive():
                break
            self._run_sql("CREATE USER 'bkpuser'@'localhost' IDENTIFIED BY 's3cret';")
            self._run_sql("GRANT RELOAD, LOCK TABLES, PROCESS, REPLICATION CLIENT ON *.* TO 'bkpuser'@'localhost'; FLUSH PRIVILEGES;")
            self._run_sql("DROP USER 'bkpuser'@'localhost';")

    def ddl_add_drop_invisible_column(self):
        """Add and drop invisible column."""
        print("DDL: Invisible column")
        for _ in range(10):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 ADD COLUMN invisible int DEFAULT 1 invisible first;")
            self._run_sql("UPDATE test.sbtest1 SET invisible = id;")
            self._run_sql("ALTER TABLE test.sbtest1 DROP COLUMN invisible;")

    def ddl_add_drop_blob_column(self):
        """Add and drop blob column."""
        print("DDL: Blob column")
        for _ in range(30):
            if not self._is_server_alive():
                break
            self._run_sql("ALTER TABLE test.sbtest1 ADD COLUMN blob_col blob;")
            self._run_sql("UPDATE test.sbtest1 SET blob_col = c;")
            self._run_sql("UPDATE test.sbtest1 SET blob_col = NULL;")
            self._run_sql("UPDATE test.sbtest1 SET blob_col = id;")
            self._run_sql("UPDATE test.sbtest1 SET blob_col = NULL;")
            self._run_sql("ALTER TABLE test.sbtest1 DROP COLUMN blob_col;")

    def ddl_add_drop_column_instant(self):
        """Add and drop column using INSTANT algorithm."""
        print("DDL: Add/drop column instant")
        for table in ["sbtest1", "sbtest2", "sbtest3", "sbtest4", "sbtest5"]:
            for _ in range(20):
                if not self._is_server_alive():
                    break
                self._run_sql(f"ALTER TABLE test.{table} ADD COLUMN b CHAR(50) NOT NULL DEFAULT '' AFTER k, ALGORITHM=INSTANT;")
                self._run_sql(f"UPDATE test.{table} SET b = k;")
                self._run_sql(f"ALTER TABLE test.{table} DROP COLUMN b, ALGORITHM=INSTANT;")
                self._run_sql(f"TRUNCATE TABLE test.{table};")

    def ddl_add_drop_column_algorithms(self):
        """Add and drop column using different algorithms."""
        print("DDL: Add/drop column with various algorithms")
        algos = [
            ("sbtest1", "ALGORITHM=DEFAULT", "ALGORITHM=DEFAULT"),
            ("sbtest2", "ALGORITHM=INPLACE", "ALGORITHM=INPLACE"),
            ("sbtest3", "ALGORITHM=COPY", "ALGORITHM=COPY"),
            ("sbtest4", "", ""),
            ("sbtest5", "ALGORITHM=INPLACE", "ALGORITHM=COPY"),
        ]
        for table, add_algo, drop_algo in algos:
            for _ in range(20):
                if not self._is_server_alive():
                    break
                add_clause = f", {add_algo}" if add_algo else ""
                drop_clause = f", {drop_algo}" if drop_algo else ""
                self._run_sql(f"ALTER TABLE test.{table} ADD COLUMN b CHAR(50) NOT NULL DEFAULT '' AFTER k{add_clause};")
                self._run_sql(f"UPDATE test.{table} SET b = k;")
                self._run_sql(f"ALTER TABLE test.{table} DROP COLUMN b{drop_clause};")

    def run_pstress_prepare(self, tool_options: str):
        """Run pstress with --prepare to create metadata."""
        print(f"=>Run pstress to prepare metadata: {tool_options}")
        pstress_logdir = os.path.join(self.logdir, "pstress")
        log_file = os.path.join(pstress_logdir, "pstress_prepare.log")
        cmd = [
            os.path.join(self.load_tool_dir, self.pstress_binary),
        ] + tool_options.split() + [
            "--prepare",
            "--exact-initial-records",
            f"--logdir={pstress_logdir}",
            f"--socket={self.socket_path}",
        ]
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: pstress prepare failed. Check {log_file}")
        print("..Metadata created")

    def take_full_backup(self, log_date: str) -> None:
        """Take a single full backup to backup_dir/full."""
        print("=>Taking full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--user=root",
            "--password=",
            "--backup",
            f"--target-dir={self.backup_dir}/full",
            f"-S{self.socket_path}",
            f"--datadir={self.datadir}",
        ] + self.backup_params.split() + ["--register-redo-log-consumer"]
        log_file = os.path.join(self.logdir, f"full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Full Backup failed. Please check the log at: {log_file}")
        print(f"..Full backup was successfully created at: {self.backup_dir}/full")

    def take_incremental_backup(
        self, inc_num: int, incremental_basedir: str, log_date: str
    ) -> None:
        """Take one incremental backup."""
        print(f"=>Taking incremental backup: {inc_num}")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--user=root",
            "--password=",
            "--backup",
            f"--target-dir={self.backup_dir}/inc{inc_num}",
            f"--incremental-basedir={incremental_basedir}",
            f"-S{self.socket_path}",
            f"--datadir={self.datadir}",
        ] + self.backup_params.split() + ["--register-redo-log-consumer"]
        log_file = os.path.join(self.logdir, f"inc{inc_num}_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
        print(f"..Inc backup was successfully created at: {self.backup_dir}/inc{inc_num}")

    def crash_and_save_datadir(self, save_name: str) -> None:
        """Kill the server with SIGKILL and copy datadir to a sibling directory."""
        print("Crash the mysql server")
        if self.mysql_pid:
            try:
                os.kill(self.mysql_pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self.mysql_pid = None
        time.sleep(1)
        crash_save_path = os.path.join(os.path.dirname(self.datadir), save_name)
        if os.path.exists(crash_save_path):
            shutil.rmtree(crash_save_path)
        shutil.copytree(self.datadir, crash_save_path)

    def _run_crash_flow(self, load_options: str, log_date: str) -> None:
        """
        Shared crash test flow: pstress prepare, full backup, crash, incremental backups,
        prepare chain, restore, optional binlog apply, check_tables.
        Assumes backup_params, prepare_params, restore_params, mysqld_options are set and
        initialize_db() has been called (server is running).
        """
        self.run_pstress_prepare(load_options)
        self.run_load(load_options + " --step 2")

        self.take_full_backup(log_date)
        if os.path.exists(f"{self.backup_dir}/full_save"):
            shutil.rmtree(f"{self.backup_dir}/full_save")
        shutil.copytree(f"{self.backup_dir}/full", f"{self.backup_dir}/full_save")
        time.sleep(1)

        self.crash_and_save_datadir("data_crash_save1")
        self.start_server()
        self.run_load(load_options + " --step 3")

        for inc_num in range(1, 5):
            base = f"{self.backup_dir}/full" if inc_num == 1 else f"{self.backup_dir}/inc{inc_num - 1}"
            self.take_incremental_backup(inc_num, base, log_date)
            inc_save = f"{self.backup_dir}/inc{inc_num}_save"
            if os.path.exists(inc_save):
                shutil.rmtree(inc_save)
            shutil.copytree(f"{self.backup_dir}/inc{inc_num}", inc_save)

        self.crash_and_save_datadir("data_crash_save2")
        self.start_server()
        self.run_load(load_options + " --step 4")

        for inc_num in range(5, 9):
            base = f"{self.backup_dir}/inc{inc_num - 1}"
            self.take_incremental_backup(inc_num, base, log_date)
            inc_save = f"{self.backup_dir}/inc{inc_num}_save"
            if os.path.exists(inc_save):
                shutil.rmtree(inc_save)
            shutil.copytree(f"{self.backup_dir}/inc{inc_num}", inc_save)

        self.prepare_crash_backup_chain(8, log_date)

        print("Collecting existing table count")
        orig_data = self.count_rows()

        print("Stopping mysql server and moving data directory")
        subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysqladmin"),
                "-uroot",
                f"-S{self.socket_path}",
                "shutdown",
            ],
            check=True,
        )
        data_orig = os.path.join(
            os.path.dirname(self.datadir),
            f"data_orig_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )
        if os.path.exists(data_orig):
            shutil.rmtree(data_orig)
        shutil.move(self.datadir, data_orig)

        print("=>Restoring full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--copy-back",
            f"--target-dir={self.backup_dir}/full",
            f"--datadir={self.datadir}",
        ] + self.restore_params.split()
        log_file = os.path.join(self.logdir, f"res_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Restore of full backup failed. Please check the log at: {log_file}")
        self.start_server()

        if (
            "binlog-encryption" not in self.mysqld_options
            and "encrypt-binlog" not in self.mysqld_options
            and "skip-log-bin" not in self.mysqld_options
        ):
            print("Check xtrabackup for binlog position")
            binlog_info_file = os.path.join(self.backup_dir, "full/xtrabackup_binlog_info")
            with open(binlog_info_file, "r") as f:
                line = f.readline().strip()
                parts = line.split()
                xb_binlog_file = parts[0] if parts else ""
                xb_binlog_pos = parts[1] if len(parts) > 1 else ""
            print(f"Xtrabackup binlog position: {xb_binlog_file}, {xb_binlog_pos}")
            print(f"Applying binlog to restored data starting from {xb_binlog_file}, {xb_binlog_pos}")
            binlog_path = os.path.join(data_orig, xb_binlog_file)
            if os.path.exists(binlog_path):
                mysqlbinlog = subprocess.Popen(
                    [
                        os.path.join(self.mysqldir, "bin/mysqlbinlog"),
                        binlog_path,
                        f"--start-position={xb_binlog_pos}",
                    ],
                    stdout=subprocess.PIPE,
                )
                mysql = subprocess.Popen(
                    [
                        os.path.join(self.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{self.socket_path}",
                    ],
                    stdin=mysqlbinlog.stdout,
                )
                mysqlbinlog.stdout.close()
                mysql.communicate()
                if mysql.returncode != 0:
                    print("ERR: The binlog could not be applied to the restored data")
            time.sleep(5)
            print("Collecting table count after restore")
            res_data = self.count_rows()
            if orig_data != res_data:
                print("ERR: Data changed after restore.")
                print("Original data:")
                print(orig_data)
                print("Restored data:")
                print(res_data)
                pytest.fail("Data mismatch after restore")
            print("Data is the same before and after restore: Pass")
        else:
            print("Binlog applying skipped, ignore differences between actual data and restored data")

        self.check_tables()

    def prepare_crash_backup_chain(self, num_incremental: int, log_date: str) -> None:
        """Prepare full backup then incremental 1..num_incremental (last one without --apply-log-only)."""
        print("=>Preparing full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--prepare",
            "--apply-log-only",
            f"--target_dir={self.backup_dir}/full",
        ] + self.prepare_params.split()
        log_file = os.path.join(self.logdir, f"prepare_full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Prepare of full backup failed. Please check the log at: {log_file}")

        for i in range(1, num_incremental + 1):
            print(f"=>Preparing incremental backup: {i}")
            if i == num_incremental:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults",
                    "--prepare",
                    f"--target_dir={self.backup_dir}/full",
                    f"--incremental-dir={self.backup_dir}/inc{i}",
                ] + self.prepare_params.split()
            else:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults",
                    "--prepare",
                    "--apply-log-only",
                    f"--target_dir={self.backup_dir}/full",
                    f"--incremental-dir={self.backup_dir}/inc{i}",
                ] + self.prepare_params.split()
            log_file = os.path.join(self.logdir, f"prepare_inc{i}_backup_{log_date}_log")
            result = self.run_command(cmd, check=False, log_file=log_file)
            if result.returncode != 0:
                pytest.fail(f"ERR: Prepare of incremental backup failed. Please check the log at: {log_file}")

    def run_keyring_plugin_backup(self, page_tracking: bool = False) -> None:
        """
        Run backup with keyring_file plugin (encryption).
        Keyring plugin is not supported in 8.4+. page_tracking: if True, add --page-tracking to backup params.
        """
        if not self.version or not self.version_normalized:
            self.version, self.version_normalized = self.get_mysql_version()
        if not self.server_type:
            self.get_mysql_type()

        if self.version_normalized >= 80400:
            pytest.skip(
                f"Keyring plugin not supported in 8.4+ (detected version: {self.version}, normalized: {self.version_normalized})"
            )
        if page_tracking and self.version_normalized < 80000:
            pytest.skip("Page Tracking is not supported in MS/PS 5.7")

        self.backup_params = f"--keyring_file_data={self.logdir}/keyring --xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin {CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
        if page_tracking:
            self.backup_params += " --page-tracking"
        self.prepare_params = f"--keyring_file_data={self.logdir}/keyring --xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin {CORE_FILE_OPT}"
        self.restore_params = self.prepare_params

        if self.version_normalized >= 80000:
            if self.server_type == "MS":
                self.mysqld_options = f"--early-plugin-load=keyring_file.so --keyring_file_data={self.logdir}/keyring --innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON --max-connections=5000 --binlog-encryption"
                tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --undo-tbs-sql 0 --no-column-compression"
            else:
                self.mysqld_options = f"--early-plugin-load=keyring_file.so --keyring_file_data={self.logdir}/keyring --innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files --table-encryption-privilege-check=ON --max-connections=5000"
                tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --undo-tbs-sql 0"
        else:
            if self.server_type == "MS":
                self.mysqld_options = f"--log-bin=binlog --early-plugin-load=keyring_file.so --keyring_file_data={self.logdir}/keyring --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
                tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --undo-tbs-sql 0 --no-ddl --no-column-compression"
            else:
                self.mysqld_options = f"--log-bin=binlog --early-plugin-load=keyring_file.so --keyring_file_data={self.logdir}/keyring --innodb-encrypt-tables=ON --encrypt-binlog --encrypt-tmp-files --innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
                tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --undo-tbs-sql 0 --no-temp-tables"

        self.initialize_db()
        if page_tracking:
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-e",
                    "INSTALL COMPONENT 'file://component_mysqlbackup';",
                ],
                check=True,
            )
        self.run_load(tool_options)
        self.take_backup()
        self.check_tables()

    def run_keyring_component_backup(self, page_tracking: bool = False) -> None:
        """
        Run backup with keyring_file component (encryption).
        page_tracking: if True, add --page-tracking to backup params.
        """
        if not self.version or not self.version_normalized:
            self.version, self.version_normalized = self.get_mysql_version()
        if self.version_normalized < 80000:
            pytest.skip("Component not supported in 5.7")
        if not self.server_type:
            self.get_mysql_type()

        manifest_file = os.path.join(self.mysqldir, "bin/mysqld.my")
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write('{\n  "components": "file://component_keyring_file"\n}\n')

        config_file = os.path.join(self.mysqldir, "lib/plugin/component_keyring_file.cnf")
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(f'{{\n  "path": "{self.logdir}/keyring",\n  "read_only": false\n}}\n')

        self.backup_params = f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin {CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
        if page_tracking:
            self.backup_params += " --page-tracking"
        self.prepare_params = f"{self.backup_params} --component-keyring-config={config_file}"
        self.restore_params = self.backup_params

        if self.server_type == "MS":
            self.mysqld_options = "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON --max-connections=5000 --binlog-encryption"
            tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds 50 --undo-tbs-sql 0 --no-column-compression"
        else:
            self.mysqld_options = "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files --table-encryption-privilege-check=ON --max-connections=5000"
            tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds 50 --undo-tbs-sql 0"

        self.initialize_db()
        if page_tracking:
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-e",
                    "INSTALL COMPONENT 'file://component_mysqlbackup';",
                ],
                check=True,
            )
        self.run_load(tool_options)
        self.take_backup()
        self.check_tables()

    def run_kmip_component_backup(self, vault_type: str) -> None:
        """
        Run backup with keyring_kmip component for the given vault type.
        Starts the KMIP server for vault_type, creates manifest and config, then runs backup flow.
        """
        if not self.version or not self.version_normalized:
            self.version, self.version_normalized = self.get_mysql_version()
        if not self.server_type:
            self.get_mysql_type()

        if self.version_normalized < 80000:
            pytest.skip("KMIP component is not supported in MS/PS 5.7")
        if self.server_type == "MS":
            pytest.skip("MS 8.0 does not support keyring kmip for encryption, skipping keyring kmip tests")

        if not KMIPHelper:
            pytest.skip("KMIP helper not available (kmip_helper module)")

        if vault_type not in KMIP_CONFIGS:
            pytest.skip(f"Unknown vault_type '{vault_type}'. Available: {list(KMIP_CONFIGS.keys())}")

        if vault_type == "fortanix" and (
            not os.environ.get("FORTANIX_EMAIL", "").strip() or not os.environ.get("FORTANIX_PASSWORD", "").strip()
        ):
            pytest.skip("Fortanix KMIP requires FORTANIX_EMAIL and FORTANIX_PASSWORD environment variables")

        if not self.kmip_helper:
            self.kmip_helper = KMIPHelper(KMIP_CONFIGS, cert_base_dir=TEST_BASE_DIR)
        if not self.kmip_helper.start_kmip_server(vault_type):
            detail = getattr(self.kmip_helper, "last_error", None) or "unknown"
            pytest.fail(f"Failed to start KMIP server for vault_type={vault_type}. {detail}")

        manifest_file = os.path.join(self.mysqldir, "bin/mysqld.my")
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write('{\n  "components": "file://component_keyring_kmip"\n}\n')

        cert_dir = self.kmip_helper.kmip_config["cert_dir"]
        kmip_cnf_src = os.path.join(cert_dir, "component_keyring_kmip.cnf")
        kmip_cnf_dst = os.path.join(self.mysqldir, "lib/plugin/component_keyring_kmip.cnf")
        if os.path.isfile(kmip_cnf_src):
            shutil.copy2(kmip_cnf_src, kmip_cnf_dst)

        self.backup_params = f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin {CORE_FILE_OPT}"
        self.prepare_params = f"{self.backup_params} --component-keyring-config={kmip_cnf_dst}"
        self.restore_params = self.backup_params

        self.mysqld_options = "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files --table-encryption-privilege-check=ON --max-connections=5000"
        tool_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --undo-tbs-sql 0"

        self.initialize_db()
        self.run_load(tool_options)
        self.take_backup()
        self.check_tables()

    def run_kms_component_backup(self, page_tracking: bool = False) -> None:
        """
        Run backup with keyring_kms component (encryption).
        Requires KMS_KEYID, KMS_SECRET_KEY, KMS_AUTH_KEY, KMS_REGION environment variables.
        page_tracking: if True, add --page-tracking to backup params and install component_mysqlbackup.
        """
        if not self.version or not self.version_normalized:
            self.version, self.version_normalized = self.get_mysql_version()
        if not self.server_type:
            self.get_mysql_type()

        if self.version_normalized < 80000:
            pytest.skip("KMS component is not supported in MS/PS 5.7")
        if self.server_type == "MS":
            pytest.skip("MS 8.0 does not support keyring kms for encryption, skipping keyring kms tests")

        # Validate KMS env vars (read from instance attributes set in __init__ from env)
        if not (self.kms_id and self.kms_auth_key and self.kms_secret_key and self.kms_region):
            pytest.skip(
                "KMS tests require KMS_KEYID, KMS_SECRET_KEY, KMS_AUTH_KEY and KMS_REGION environment variables"
            )

        manifest_file = os.path.join(self.mysqldir, "bin/mysqld.my")
        config_file = os.path.join(self.mysqldir, "lib/plugin/component_keyring_kms.cnf")
        keyring_path = os.path.join(self.logdir, "keyring_kms")

        try:
            with open(manifest_file, "w", encoding="utf-8") as f:
                f.write('{\n  "components": "file://component_keyring_kms"\n}\n')

            with open(config_file, "w", encoding="utf-8") as f:
                f.write(
                    f'{{\n  "path": "{keyring_path}", "region": "{self.kms_region}", '
                    f'"kms_key": "{self.kms_id}", "auth_key": "{self.kms_auth_key}", '
                    f'"secret_access_key": "{self.kms_secret_key}", "read_only": false\n}}\n'
                )

            self.backup_params = (
                f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin {CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
            )
            if page_tracking:
                self.backup_params += " --page-tracking"
            self.prepare_params = f"{self.backup_params} --component-keyring-config={config_file}"
            self.restore_params = self.backup_params

            self.mysqld_options = (
                "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
                "--innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON --log-slave-updates "
                "--gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
                "--binlog_checksum=CRC32 --encrypt-tmp-files --table-encryption-privilege-check=ON --max-connections=5000"
            )
            tool_options = (
                f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} "
                f"--seconds {self.seconds} --undo-tbs-sql 0"
            )

            self.initialize_db()
            if page_tracking:
                subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{self.socket_path}",
                        "-e",
                        "INSTALL COMPONENT 'file://component_mysqlbackup';",
                    ],
                    check=True,
                )
            self.run_load(tool_options)
            self.take_backup()
            self.check_tables()
        finally:
            if os.path.exists(manifest_file):
                os.remove(manifest_file)
            if os.path.exists(config_file):
                os.remove(config_file)

    def run_crash_tests_pstress(self, storage_engine: str, page_tracking: bool) -> None:
        """
        Run crash tests with pstress: crash server during load, then backup/restore and verify.
        storage_engine: 'innodb' or 'rocksdb'
        page_tracking: if True, enable page-tracking and component_mysqlbackup.
        """
        if self.load_tool != "pstress":
            pytest.skip("Crash tests require load_tool=pstress")

        # Set options based on storage engine
        if storage_engine == "rocksdb":
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysqld"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            if "5.7" in result.stdout:
                pytest.skip("Rocksdb backup is not supported in MS/PS 5.7")
            if "MySQL Community Server" in result.stdout:
                pytest.skip("RocksDB is unsupported in MS")
            self.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
            self.backup_params = f"{CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
            self.prepare_params = f"{CORE_FILE_OPT}"
            self.restore_params = ""
            load_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --no-encryption --engine=rocksdb"
        else:
            self.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
            self.backup_params = f"{CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
            self.prepare_params = f"{CORE_FILE_OPT}"
            self.restore_params = ""
            load_options = ""  # set after initialize_db() when server_type is known

        if page_tracking:
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysqld"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            if "5.7" in result.stdout:
                pytest.skip("Page Tracking is not supported in MS/PS 5.7")
            print("Running test with page tracking enabled")
            self.backup_params = self.backup_params + " --page-tracking"

        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        os.makedirs(self.backup_dir)
        log_date = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.initialize_db()

        if storage_engine == "innodb":
            if self.server_type == "MS":
                load_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --no-encryption --undo-tbs-sql 0 --no-column-compression"
            else:
                load_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --no-encryption --undo-tbs-sql 0"

        if storage_engine == "rocksdb":
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/ps-admin"),
                    "--enable-rocksdb",
                    "-uroot",
                    f"-S{self.socket_path}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-e",
                    "CREATE DATABASE IF NOT EXISTS test",
                ],
                check=False,
            )

        if page_tracking:
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-e",
                    "INSTALL COMPONENT 'file://component_mysqlbackup';",
                ],
                check=True,
            )

        self._run_crash_flow(load_options, log_date)

    def run_crash_tests_pstress_encrypted(
        self, page_tracking: bool, vault_type: Optional[str] = None
    ) -> None:
        """
        Run crash tests with pstress and encryption (keyring_file or keyring_kmip).
        When vault_type is None, use keyring_file component; otherwise use keyring_kmip
        for the given vault type. Optionally enable page_tracking.
        """
        if self.load_tool != "pstress":
            pytest.skip("Crash tests require load_tool=pstress")

        if not self.version or not self.version_normalized:
            self.version, self.version_normalized = self.get_mysql_version()
        if not self.server_type:
            self.get_mysql_type()

        if self.version_normalized < 80000:
            pytest.skip("Encrypted crash tests require 8.0+ (component keyring)")

        # Seconds for encrypted crash test (shell uses 50)
        crash_seconds = 50
        load_options_base = (
            f"--tables {self.num_tables} --records {self.table_size} "
            f"--threads {self.threads} --seconds {crash_seconds} --undo-tbs-sql 0"
        )
        if self.server_type == "MS":
            load_options_base += " --no-column-compression --no-temp-tables"
        keyring_config_file: Optional[str] = None
        manifest_file = os.path.join(self.mysqldir, "bin/mysqld.my")

        try:
            if vault_type is None:
                # keyring_file
                print("Testing keyring_file (encrypted crash)...")
                if not os.path.exists(os.path.join(self.mysqldir, "lib/plugin")):
                    os.makedirs(os.path.join(self.mysqldir, "lib/plugin"), exist_ok=True)
                with open(manifest_file, "w", encoding="utf-8") as f:
                    f.write('{\n  "components": "file://component_keyring_file"\n}\n')
                keyring_config_file = os.path.join(
                    self.mysqldir, "lib/plugin/component_keyring_file.cnf"
                )
                with open(keyring_config_file, "w", encoding="utf-8") as f:
                    f.write(
                        f'{{\n  "path": "{self.logdir}/keyring",\n  "read_only": false\n}}\n'
                    )
                self.backup_params = (
                    f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin "
                    f"{CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
                )
                if page_tracking:
                    self.backup_params += " --page-tracking"
                self.prepare_params = (
                    f"{self.backup_params} --component-keyring-config={keyring_config_file}"
                )
                self.restore_params = self.backup_params
                if self.server_type == "MS":
                    self.mysqld_options = (
                        "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
                        "--default-table-encryption=ON --log-slave-updates --gtid-mode=ON "
                        "--enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
                        "--binlog_checksum=CRC32 --binlog-rotate-encryption-master-key-at-startup "
                        "--table-encryption-privilege-check=ON --max-connections=5000 --binlog-encryption"
                    )
                else:
                    self.mysqld_options = (
                        "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
                        "--default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON "
                        "--innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON "
                        "--enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
                        "--binlog_checksum=CRC32 --encrypt-tmp-files "
                        "--table-encryption-privilege-check=ON --max-connections=5000"
                    )
            else:
                # keyring_kmip
                if self.server_type == "MS":
                    pytest.skip(
                        "MS 8.0 does not support keyring kmip for encryption, skipping"
                    )
                if not KMIPHelper:
                    pytest.skip("KMIP helper not available (kmip_helper module)")
                if vault_type not in KMIP_CONFIGS:
                    pytest.skip(
                        f"Unknown vault_type '{vault_type}'. "
                        f"Available: {list(KMIP_CONFIGS.keys())}"
                    )
                if vault_type == "fortanix" and (
                    not os.environ.get("FORTANIX_EMAIL", "").strip()
                    or not os.environ.get("FORTANIX_PASSWORD", "").strip()
                ):
                    pytest.skip(
                        "Fortanix KMIP requires FORTANIX_EMAIL and FORTANIX_PASSWORD"
                    )
                print(f"Testing keyring_kmip with vault {vault_type} (encrypted crash)...")
                if not self.kmip_helper:
                    self.kmip_helper = KMIPHelper(KMIP_CONFIGS, cert_base_dir=TEST_BASE_DIR)
                if not self.kmip_helper.start_kmip_server(vault_type):
                    detail = getattr(self.kmip_helper, "last_error", None) or "unknown"
                    pytest.fail(
                        f"Failed to start KMIP server for vault_type={vault_type}. {detail}"
                    )
                with open(manifest_file, "w", encoding="utf-8") as f:
                    f.write('{\n  "components": "file://component_keyring_kmip"\n}\n')
                cert_dir = self.kmip_helper.kmip_config["cert_dir"]
                kmip_cnf_src = os.path.join(cert_dir, "component_keyring_kmip.cnf")
                keyring_config_file = os.path.join(
                    self.mysqldir, "lib/plugin/component_keyring_kmip.cnf"
                )
                if os.path.isfile(kmip_cnf_src):
                    shutil.copy2(kmip_cnf_src, keyring_config_file)
                self.backup_params = (
                    f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin "
                    f"{CORE_FILE_OPT} --lock-ddl={self.lock_ddl}"
                )
                if page_tracking:
                    self.backup_params += " --page-tracking"
                self.prepare_params = (
                    f"{self.backup_params} --component-keyring-config={keyring_config_file}"
                )
                self.restore_params = self.backup_params
                self.mysqld_options = (
                    "--innodb-undo-log-encrypt --innodb-redo-log-encrypt "
                    "--default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON "
                    "--innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON "
                    "--enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
                    "--binlog_checksum=CRC32 --encrypt-tmp-files "
                    "--table-encryption-privilege-check=ON --max-connections=5000"
                )

            if os.path.exists(self.backup_dir):
                shutil.rmtree(self.backup_dir)
            os.makedirs(self.backup_dir)
            log_date = datetime.now().strftime("%Y%m%d_%H%M%S")

            self.initialize_db()

            if page_tracking and self.version_normalized >= 80000:
                print("Running test with page tracking enabled")
                subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{self.socket_path}",
                        "-e",
                        "INSTALL COMPONENT 'file://component_mysqlbackup';",
                    ],
                    check=True,
                )

            self._run_crash_flow(load_options_base, log_date)
        finally:
            # Remove keyring config so later tests can run without encryption
            if os.path.isfile(manifest_file):
                try:
                    os.remove(manifest_file)
                except OSError:
                    pass
            if keyring_config_file and os.path.isfile(keyring_config_file):
                try:
                    os.remove(keyring_config_file)
                except OSError:
                    pass
            kmip_cnf = os.path.join(self.mysqldir, "lib/plugin/component_keyring_kmip.cnf")
            if os.path.isfile(kmip_cnf):
                try:
                    os.remove(kmip_cnf)
                except OSError:
                    pass

    def count_rows(self, database: str = "test") -> str:
        """Count rows and checksums of all tables in a database."""
        result = subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysql"),
                "-uroot",
                f"-S{self.socket_path}",
                "-Bse",
                f"SHOW TABLES FROM {database};",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        tables = result.stdout.strip().split("\n")
        output = []

        for table in tables:
            if table:
                # Row count
                result = subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{self.socket_path}",
                        "-Bse",
                        f"select count(*) from {database}.{table}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                output.append(f"Row count for {database}.{table}: {result.stdout.strip()}")

                # Checksum
                result = subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysql"),
                        "-uroot",
                        f"-S{self.socket_path}",
                        "-Bse",
                        f"checksum table {database}.{table}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    output.append(f"Checksum of table {database}.{table}: {parts[1]}")

        return "\n".join(output)

    def check_tables(
        self,
        socket_path: Optional[str] = None,
        basedir: Optional[str] = None,
        database: str = "test",
    ):
        """Check table integrity; by default targets the primary.

        When ``socket_path`` is given, the check is dispatched to the matching
        :class:`MySQLServer` (primary or one of the replicas).  ``basedir`` is
        accepted for backward compatibility but is not required: each
        ``MySQLServer`` already knows its own ``basedir``.
        """
        if socket_path is not None:
            for server in (self.primary, *self.replicas):
                if server.socket_path == socket_path:
                    server.check_tables(database=database)
                    return
            # Fallback: unknown socket; build a temporary server-like shim.
            shim = MySQLServer(
                name="adhoc",
                datadir="",
                socket_path=socket_path,
                port=0,
                basedir=basedir or self.mysqldir,
                error_log="",
                logdir=self.logdir,
            )
            shim.check_tables(database=database)
            return
        self.primary.check_tables(database=database)

    def cleanup(self):
        """Cleanup function."""
        print("\n################################## CleanUp #######################################")

        # Destroy replicas first (stop mysqld + remove each datadir).
        for replica in list(self.replicas):
            try:
                replica.destroy()
            except Exception as exc:  # noqa: BLE001
                print(f"  Ignoring error destroying replica '{replica.name}': {exc}")
        self.replicas.clear()

        # Destroy the primary (stop mysqld + remove its datadir).
        try:
            self.primary.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"  Ignoring error destroying primary: {exc}")

        print("Killing any previously running mysqld process")

        # Kill mysqld processes
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if "mysqld" in proc.info["name"].lower():
                    cmdline = proc.info.get("cmdline", [])
                    if cmdline and "error.log" in " ".join(cmdline):
                        proc.kill()
                        proc.wait()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if self.mysql_pid:
            try:
                os.kill(self.mysql_pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

        if os.path.exists(self.datadir):
            print("=>Found previously existing data directory")
            shutil.rmtree(self.datadir)
            print("..Deleted")

        # Clean crash-test save directories (siblings of datadir)
        for name in ("data_crash_save1", "data_crash_save2"):
            crash_save = os.path.join(os.path.dirname(self.datadir), name)
            if os.path.exists(crash_save):
                print(f"=>Removing {crash_save}")
                shutil.rmtree(crash_save)
                print("..Deleted")
        try:
            for entry in os.listdir(os.path.dirname(self.datadir)):
                if entry.startswith("data_orig_"):
                    path = os.path.join(os.path.dirname(self.datadir), entry)
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                        print(f"..Removed {path}")
        except OSError:
            pass

        if os.path.exists(os.path.join(self.mysqldir, "bin/mysqld.my")):
            print("=>Found older manifest file in mysql bin directory")
            os.remove(os.path.join(self.mysqldir, "bin/mysqld.my"))
            print("..Deleted")

        if os.path.exists(os.path.join(self.mysqldir, "lib/plugin/component_keyring_file.cnf")):
            print("=>Found older keyring_component config file in lib/plugin directory")
            os.remove(os.path.join(self.mysqldir, "lib/plugin/component_keyring_file.cnf"))
            print("..Deleted")

        if os.path.exists(os.path.join(self.mysqldir, "lib/plugin/component_keyring_kms.cnf")):
            print("=>Found older keyring_kms config file in lib/plugin directory")
            os.remove(os.path.join(self.mysqldir, "lib/plugin/component_keyring_kms.cnf"))
            print("..Deleted")

        if os.path.exists(os.path.join(self.logdir, "keyfile")):
            print("=>Found older keyring_component keyfile in lib/plugin directory")
            os.remove(os.path.join(self.logdir, "keyfile"))
            print("..Deleted")

        if os.path.exists(os.path.join(self.logdir, "keyring_kms")):
            print("=>Found older keyring_kms file in backuplogs directory")
            os.remove(os.path.join(self.logdir, "keyring_kms"))
            print("..Deleted")

        # Cleanup KMIP containers
        print("Checking for previously started containers...")
        if KMIPHelper:
            if not self.kmip_helper:
                self.kmip_helper = KMIPHelper(KMIP_CONFIGS)
            self.kmip_helper.get_kmip_container_names()
            containers_found = False

            for name in self.kmip_helper.kmip_container_names:
                result = subprocess.run(
                    ["docker", "ps", "-aq", "--filter", f"name={name}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.stdout.strip():
                    containers_found = True
                    break

            if containers_found:
                print("Killing previously started containers if any...")
                for name in self.kmip_helper.kmip_container_names:
                    self.kmip_helper.cleanup_existing_container(name)

        # Stop vault server and cleanup vault directory
        self.stop_vault_server()
        vault_dir = os.path.join(HOME, "vault")
        if os.path.exists(vault_dir) and HOME:
            print("Cleaning up vault directory...")
            try:
                subprocess.run(["sudo", "rm", "-rf", vault_dir], check=False)
            except Exception:
                pass


    # Additional test methods would go here - they are implemented as pytest test functions below
    # to maintain pytest structure

