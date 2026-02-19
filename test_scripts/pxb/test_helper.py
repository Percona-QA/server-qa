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
from datetime import datetime
from typing import Optional, List, Tuple
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

# KMIP Configurations
KMIP_CONFIGS = {
    "pykmip": "addr=127.0.0.1,image=mohitpercona/kmip:latest,port=5696,name=kmip_pykmip",
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

        # Create test-specific directories with test name
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if test_name:
            # Use test name in directory names
            test_suffix = test_name.replace("test_", "").replace("_", "-")
            self.datadir = datadir or os.path.join(TEST_BASE_DIR, f"data_{test_suffix}_{timestamp}")
            self.backup_dir = backup_dir or os.path.join(TEST_BASE_DIR, f"dbbackup_{test_suffix}_{timestamp}")
            self.logdir = logdir or os.path.join(TEST_BASE_DIR, f"backuplogs_{test_suffix}_{timestamp}")
            self.socket_path = os.path.join(TEST_BASE_DIR, f"socket_{test_suffix}.sock")
        else:
            # Fallback to timestamp-only if no test name provided
            self.datadir = datadir or os.path.join(TEST_BASE_DIR, f"data_{timestamp}")
            self.backup_dir = backup_dir or os.path.join(TEST_BASE_DIR, f"dbbackup_{timestamp}")
            self.logdir = logdir or os.path.join(TEST_BASE_DIR, f"backuplogs_{timestamp}")
            self.socket_path = os.path.join(TEST_BASE_DIR, "socket.sock")

        # Runtime variables
        self.server_type: Optional[str] = None
        self.version: Optional[str] = None
        self.version_normalized: Optional[int] = None
        self.pstress_binary: Optional[str] = None
        self.mysql_pid: Optional[int] = None
        self.mysqld_options: str = ""
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
            if log_file and result.stdout:
                with open(log_file, "a") as f:
                    f.write(result.stdout)
            return result

    def start_server(self):
        """Start MySQL server."""
        print("=>Starting MySQL server")
        mysqld_path = os.path.join(self.mysqldir, "bin/mysqld")
        cmd = (["rr", mysqld_path] if USE_RR else [mysqld_path]) + [
            "--no-defaults",
            f"--basedir={self.mysqldir}",
            f"--datadir={self.datadir}",
        ] + self.mysqld_options.split() + [
            "--port=21000",
            f"--socket={self.socket_path}",
            f"--plugin-dir={self.mysqldir}/lib/plugin",
            "--max-connections=1024",
            f"--log-error={self.logdir}/error.log",
            "--general-log",
            "--log-error-verbosity=3",
            "--core-file",
        ]
        print(f"=>Command for starting server: {' '.join(cmd)}")

        process = self.run_command(cmd, check=False, background=True)
        self.mysql_pid = process.pid

        # Wait for server to start
        for x in range(self.mysql_start_timeout + 1):
            time.sleep(1)
            try:
                result = subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysqladmin"),
                        "-uroot",
                        f"-S{self.socket_path}",
                        "ping",
                    ],
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    print("..Server started successfully")
                    return
            except Exception:
                pass

            if x == self.mysql_start_timeout:
                pytest.fail(
                    f"ERR: Database could not be started. Please check error logs: {self.logdir}/error.log"
                )

    def initialize_db(self):
        """Initialize and start MySQL database."""
        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir)

        print("=>Creating data directory")
        log_file = os.path.join(self.logdir, "mysql_install_db.log")
        with open(log_file, "w") as f:
            subprocess.run(
                [
                    os.path.join(self.mysqldir, "bin/mysqld"),
                    "--no-defaults",
                    f"--datadir={self.datadir}",
                    "--initialize-insecure",
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )
        print("..Data directory created")

        self.start_server()

        # Drop and create test database
        subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysql"),
                "-uroot",
                f"-S{self.socket_path}",
                "-e",
                "DROP DATABASE IF EXISTS test",
            ],
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
        if self.load_tool == "sysbench":
            if "keyring" not in self.mysqld_options:
                subprocess.run(
                    [
                        "sysbench",
                        "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}",
                        f"--table-size={self.table_size}",
                        "--mysql-db=test",
                        "--mysql-user=root",
                        "--threads=50",
                        "--db-driver=mysql",
                        f"--mysql-socket={self.socket_path}",
                        "prepare",
                    ],
                    stdout=open(os.path.join(self.logdir, "sysbench.log"), "w"),
                    check=True,
                )
            else:
                # Encryption enabled
                for i in range(1, self.num_tables + 1):
                    print(f"Creating the table sbtest{i}...")
                    subprocess.run(
                        [
                            os.path.join(self.mysqldir, "bin/mysql"),
                            "-uroot",
                            f"-S{self.socket_path}",
                            "-e",
                            f"CREATE TABLE test.sbtest{i} (id int(11) NOT NULL AUTO_INCREMENT, k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y';",
                        ],
                        check=True,
                    )

    def run_load(self, tool_options: str):
        """Run a load using pstress/sysbench."""
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
            print("Run sysbench")
            cmd = [
                "sysbench",
                "/usr/share/sysbench/oltp_insert.lua",
                f"--tables={self.num_tables}",
                "--mysql-db=test",
                "--mysql-user=root",
                "--threads=50",
                "--db-driver=mysql",
                f"--mysql-socket={self.socket_path}",
                f"--time={self.seconds}",
                "run",
            ]
            log_file = os.path.join(self.logdir, "sysbench.log")
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

    def take_backup(self):
        """Take incremental backup."""
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        os.makedirs(self.backup_dir)

        log_date = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Full backup
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
        print(f"=>Command for executing full backup: {' '.join(cmd)}")

        log_file = os.path.join(self.logdir, f"full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            print(f"=>Backup failed: {' '.join(result.stdout)}")
            print(result.stderr)
            pytest.fail(f"ERR: Full Backup failed. Please check the log at: {log_file}")
        else:
            print(f"..Full backup was successfully created at: {self.backup_dir}/full. Logs available at: {log_file}")

        time.sleep(1)

        # Incremental backups
        inc_num = 1
        while self.is_load_running():
            print(f"=>Taking incremental backup: {inc_num}")
            if inc_num == 1:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults",
                    "--user=root",
                    "--password=",
                    "--backup",
                    f"--target-dir={self.backup_dir}/inc{inc_num}",
                    f"--incremental-basedir={self.backup_dir}/full",
                    f"-S{self.socket_path}",
                    f"--datadir={self.datadir}",
                ] + self.backup_params.split() + ["--register-redo-log-consumer"]
            else:
                cmd = self._xtrabackup_cmd_prefix() + [
                    "--no-defaults",
                    "--user=root",
                    "--password=",
                    "--backup",
                    f"--target-dir={self.backup_dir}/inc{inc_num}",
                    f"--incremental-basedir={self.backup_dir}/inc{inc_num - 1}",
                    f"-S{self.socket_path}",
                    f"--datadir={self.datadir}",
                ] + self.backup_params.split() + ["--register-redo-log-consumer"]
            print(f"=>Command for executing incremental backup: {inc_num}: {' '.join(cmd)}")

            log_file = os.path.join(self.logdir, f"inc{inc_num}_backup_{log_date}_log")
            result = self.run_command(cmd, check=False, log_file=log_file)

            if result.returncode != 0:
                # Check for retry condition
                with open(log_file, "r") as f:
                    log_content = f.read()
                    if "PXB will not be able to make a consistent backup" in log_content or "PXB will not be able to take a consistent backup" in log_content:
                        print("Retrying incremental backup with --lock-ddl option")
                        if os.path.exists(f"{self.backup_dir}/inc{inc_num}"):
                            shutil.rmtree(f"{self.backup_dir}/inc{inc_num}")

                        if inc_num == 1:
                            cmd = self._xtrabackup_cmd_prefix() + [
                                "--no-defaults",
                                "--user=root",
                                "--password=",
                                "--backup",
                                f"--target-dir={self.backup_dir}/inc{inc_num}",
                                f"--incremental-basedir={self.backup_dir}/full",
                                f"-S{self.socket_path}",
                                f"--datadir={self.datadir}",
                            ] + self.backup_params.split() + [f"--lock-ddl={self.lock_ddl}", "--register-redo-log-consumer"]
                        else:
                            cmd = self._xtrabackup_cmd_prefix() + [
                                "--no-defaults",
                                "--user=root",
                                "--password=",
                                "--backup",
                                f"--target-dir={self.backup_dir}/inc{inc_num}",
                                f"--incremental-basedir={self.backup_dir}/inc{inc_num - 1}",
                                f"-S{self.socket_path}",
                                f"--datadir={self.datadir}",
                            ] + self.backup_params.split() + [f"--lock-ddl={self.lock_ddl}", "--register-redo-log-consumer"]
                        print(f"=>Command for executing incremental backup: {inc_num}: {' '.join(cmd)}")

                        result = self.run_command(cmd, check=False, log_file=log_file)
                        if result.returncode != 0:
                            pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
                    else:
                        pytest.fail(f"ERR: Incremental Backup failed. Please check the log at: {log_file}")
            else:
                print(f"..Inc backup was successfully created at: {self.backup_dir}/inc{inc_num}. Logs available at: {log_file}")

            inc_num += 1
            time.sleep(10)  # Sleep before next backup

        # Prepare backups
        print("=>Preparing full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--prepare",
            "--apply-log-only",
            f"--target_dir={self.backup_dir}/full",
        ] + self.prepare_params.split()
        print(f"=>Command for executing backup preparation: {' '.join(cmd)}")

        log_file = os.path.join(self.logdir, f"prepare_full_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Prepare of full backup failed. Please check the log at: {log_file}")
        else:
            print(f"..Prepare of full backup was successful. Logs available at: {log_file}")

        for i in range(1, inc_num):
            print(f"=>Preparing incremental backup: {i}")
            if i == inc_num - 1:
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
            print(f"=>Command for executing incremental backup preparation: {' '.join(cmd)}")

            log_file = os.path.join(self.logdir, f"prepare_inc{i}_backup_{log_date}_log")
            result = self.run_command(cmd, check=False, log_file=log_file)
            if result.returncode != 0:
                pytest.fail(f"ERR: Prepare of incremental backup failed. Please check the log at: {log_file}")
            else:
                print(f"..Prepare of incremental backup was successful. Logs available at: {log_file}")

        # Collect table count before restore
        print("Collecting existing table count")
        old_cwd = os.getcwd()
        os.chdir(self.logdir)
        try:
            with open("file1", "w") as f:
                result = subprocess.run(
                    [
                        "pt-table-checksum",
                        f"S={self.socket_path},u=root",
                        "-d",
                        "test",
                        "--recursion-method",
                        "none",
                        "--no-check-binlog-format",
                    ],
                    stdout=f,
                    check=False,
                )
                if result.returncode not in (0, 64):
                    raise subprocess.CalledProcessError(result.returncode, result.args)
            # Extract table and checksum
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

        # Stop server and move data directory
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

        data_orig = os.path.join(self.backup_dir, f"data_orig_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        if os.path.exists(data_orig):
            shutil.rmtree(data_orig)
        shutil.move(self.datadir, data_orig)

        # Restore backup
        print("=>Restoring full backup")
        cmd = self._xtrabackup_cmd_prefix() + [
            "--no-defaults",
            "--copy-back",
            f"--target-dir={self.backup_dir}/full",
            f"--datadir={self.datadir}",
        ] + self.restore_params.split()
        print(f"=>Command for executing backup restoration: {' '.join(cmd)}")

        log_file = os.path.join(self.logdir, f"res_backup_{log_date}_log")
        result = self.run_command(cmd, check=False, log_file=log_file)
        if result.returncode != 0:
            pytest.fail(f"ERR: Restore of full backup failed. Please check the log at: {log_file}")
        else:
            print(f"..Restore of full backup was successful. Logs available at: {log_file}")

        self.start_server()

        # Apply binlog if not encrypted
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

            # Collect table count after restore
            print("Collecting table count after restore")
            old_cwd = os.getcwd()
            os.chdir(self.logdir)
            try:
                with open("file2", "w") as f:
                    result = subprocess.run(
                        [
                            "pt-table-checksum",
                            f"S={self.socket_path},u=root",
                            "-d",
                            "test",
                            "--recursion-method",
                            "none",
                            "--no-check-binlog-format",
                        ],
                        stdout=f,
                        check=False,
                    )
                    if result.returncode not in (0, 64):
                        raise subprocess.CalledProcessError(result.returncode, result.args)
                # Extract table and checksum
                with open("file2", "r") as f:
                    lines = f.readlines()
                with open("file2", "w") as f:
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 9:
                            f.write(f"{parts[3]} {parts[8]}\n")

                # Compare files
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

        self.backup_params = f"--keyring_file_data={self.logdir}/keyring --xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin --core-file --lock-ddl={self.lock_ddl}"
        if page_tracking:
            self.backup_params += " --page-tracking"
        self.prepare_params = f"--keyring_file_data={self.logdir}/keyring --xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin --core-file"
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

        self.backup_params = f"--xtrabackup-plugin-dir={self.xtrabackup_dir}/../lib/plugin --core-file --lock-ddl={self.lock_ddl}"
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
            self.backup_params = f"--core-file --lock-ddl={self.lock_ddl}"
            self.prepare_params = "--core-file"
            self.restore_params = ""
            load_options = f"--tables {self.num_tables} --records {self.table_size} --threads {self.threads} --seconds {self.seconds} --no-encryption --engine=rocksdb"
        else:
            self.mysqld_options = "--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
            self.backup_params = f"--core-file --lock-ddl={self.lock_ddl}"
            self.prepare_params = "--core-file"
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

    def check_tables(self):
        """Check the tables in a database."""
        print("Check the table status")
        check_err = 0

        result = subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysql"),
                "-uroot",
                f"-S{self.socket_path}",
                "-Bse",
                "SHOW TABLES FROM test;",
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
                    os.path.join(self.mysqldir, "bin/mysql"),
                    "-uroot",
                    f"-S{self.socket_path}",
                    "-Bse",
                    f"CHECK TABLE test.{table}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                print(f"ERR: CHECK TABLE test.{table} query failed")
                # Check if database went down
                result = subprocess.run(
                    [
                        os.path.join(self.mysqldir, "bin/mysqladmin"),
                        "ping",
                        "--user=root",
                        f"--socket={self.socket_path}",
                    ],
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    pytest.fail(f"ERR: The database has gone down due to corruption in table test.{table}")

            table_status = result.stdout.strip()
            if "OK" not in table_status:
                print(f"ERR: CHECK TABLE test.{table} query displayed the table status as '{table_status}'")
                check_err = 1

        # Check if database went down
        result = subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysqladmin"),
                "ping",
                "--user=root",
                f"--socket={self.socket_path}",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail("ERR: The database has gone down due to corruption, the restore was unsuccessful")

        if check_err == 0:
            print("All innodb tables status: OK")
        else:
            print("After restore, some tables may be corrupt, check table status is not OK")

    def cleanup(self):
        """Cleanup function."""
        print("\n################################## CleanUp #######################################")
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

        if os.path.exists(os.path.join(self.logdir, "keyfile")):
            print("=>Found older keyring_component keyfile in lib/plugin directory")
            os.remove(os.path.join(self.logdir, "keyfile"))
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

        # Cleanup vault directory
        vault_dir = os.path.join(HOME, "vault")
        if os.path.exists(vault_dir) and HOME:
            print("Cleaning up vault directory...")
            try:
                subprocess.run(["sudo", "rm", "-rf", vault_dir], check=False)
            except Exception:
                pass


    # Additional test methods would go here - they are implemented as pytest test functions below
    # to maintain pytest structure

