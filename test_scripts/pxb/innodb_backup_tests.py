#!/usr/bin/env python3
"""
This script tests backup for innodb tables
Assumption: PS8.0 and PXB8.0 are already installed
Usage:
1. Set paths in this script:
   xtrabackup_dir, backup_dir, mysqldir, datadir, qascripts, logdir
2. Run the script as: pytest innodb_backup_tests.py
3. Logs are available in: logdir
"""

import os
import sys
import subprocess
import time
import shutil
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple
import pytest

# Set script variables
HOME = os.path.expanduser("~")
XTRABACKUP_DIR = os.path.join(HOME, "pxb-3034-repo/bld_debug/install/bin")
MYSQLDIR = os.path.join(HOME, "Percona-Server-8.2.0-1-Linux.x86_64.glibc2.31")
DATADIR = os.path.join(MYSQLDIR, "data")
BACKUP_DIR = os.path.join(HOME, f"dbbackup_{datetime.now().strftime('%d_%m_%Y')}")
QASCRIPTS = os.path.join(HOME, "server-qa")
LOGDIR = os.path.join(HOME, "backuplogs")

# Set sysbench variables
NUM_TABLES = 10
TABLE_SIZE = 1000
LOCK_DDL = "on"  # Accepted values: reduced, on


class InnoDBBackupTestHelper:
    """Helper class for InnoDB backup tests."""

    def __init__(
        self,
        xtrabackup_dir: str = XTRABACKUP_DIR,
        mysqldir: str = MYSQLDIR,
        datadir: str = DATADIR,
        backup_dir: str = BACKUP_DIR,
        qascripts: str = QASCRIPTS,
        logdir: str = LOGDIR,
        num_tables: int = NUM_TABLES,
        table_size: int = TABLE_SIZE,
        lock_ddl: str = LOCK_DDL,
    ):
        """Initialize test helper with configuration."""
        self.xtrabackup_dir = xtrabackup_dir
        self.mysqldir = mysqldir
        self.datadir = datadir
        self.backup_dir = backup_dir
        self.qascripts = qascripts
        self.logdir = logdir
        self.num_tables = num_tables
        self.table_size = table_size
        self.lock_ddl = lock_ddl
        self.socket = os.path.join(mysqldir, "socket.sock")
        self.startup_script = os.path.join(qascripts, "startup.sh")

    def run_command(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = False,
        cwd: Optional[str] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run a shell command and return the result."""
        try:
            result = subprocess.run(
                cmd,
                check=check,
                capture_output=capture_output,
                text=True,
                cwd=cwd,
                input=input,
            )
            return result
        except subprocess.CalledProcessError as e:
            print(f"ERR: Command failed: {' '.join(cmd)}")
            print(f"Error: {e}")
            if e.stdout:
                print(f"stdout: {e.stdout}")
            if e.stderr:
                print(f"stderr: {e.stderr}")
            raise

    def check_mysql_running(self) -> bool:
        """Check if MySQL server is running."""
        try:
            self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysqladmin"),
                    "ping",
                    "--user=root",
                    f"--socket={self.socket}",
                ],
                check=False,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def wait_for_mysql(self, timeout: int = 120) -> bool:
        """Wait for MySQL server to be ready."""
        for _ in range(timeout * 4):  # Check every 0.25 seconds
            if self.check_mysql_running():
                return True
            time.sleep(0.25)
        return False

    def initialize_db(self, mysqld_options: str = ""):
        """Initialize, start and create the mysql database."""
        print("Starting mysql database")
        original_cwd = os.getcwd()
        try:
            os.chdir(self.mysqldir)
            all_no_cl = os.path.join(self.mysqldir, "all_no_cl")
            if not os.path.exists(all_no_cl):
                # Run startup.sh script
                startup_script = self.startup_script
                if not os.path.exists(startup_script):
                    raise FileNotFoundError(
                        f"startup.sh not found at {startup_script}"
                    )
                self.run_command(["bash", startup_script], cwd=self.mysqldir)

            # Start MySQL with all_no_cl (run via bash since it doesn't have shebang)
            cmd = ["bash", "./all_no_cl", "--log-bin=binlog"]
            if mysqld_options:
                cmd.extend(mysqld_options.split())
            self.run_command(
                cmd,
                check=False,
                capture_output=True,
            )

            if not self.wait_for_mysql():
                raise RuntimeError(
                    f"Database could not be started in location {self.mysqldir}. "
                    "Please check the directory"
                )

            print("Creating innodb data in database")
            # Check if sysbench is available
            sysbench_check = self.run_command(
                ["which", "sysbench"], check=False, capture_output=True
            )
            if sysbench_check.returncode != 0:
                raise RuntimeError("ERR: Sysbench not found, data could not be created")

            # Set root password authentication
            self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-e",
                    "ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '';",
                ]
            )

            # Create tables
            if "encrypt" not in mysqld_options:
                # Create tables without encryption
                self.run_command(
                    [
                        "sysbench",
                        "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}",
                        f"--table-size={self.table_size}",
                        "--mysql-db=test",
                        "--mysql-user=root",
                        "--threads=100",
                        "--db-driver=mysql",
                        f"--mysql-socket={self.socket}",
                        "prepare",
                    ]
                )
            else:
                # Create encrypted tables
                sysbench_result = self.run_command(
                    [
                        "sysbench",
                        "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}",
                        f"--table-size={self.table_size}",
                        "--mysql-db=test",
                        "--mysql-user=root",
                        "--threads=100",
                        "--db-driver=mysql",
                        f"--mysql-socket={self.socket}",
                        "--mysql-table-options=Encryption='Y'",
                        "prepare",
                    ],
                    check=False,
                )
                if sysbench_result.returncode != 0:
                    # Create tables manually
                    for i in range(1, self.num_tables + 1):
                        print(f"Creating the table sbtest{i}...")
                        self.run_command(
                            [
                                os.path.join(self.mysqldir, "bin", "mysql"),
                                "-uroot",
                                f"-S{self.socket}",
                                "-e",
                                f"CREATE TABLE test.sbtest{i} (id int(11) NOT NULL AUTO_INCREMENT, "
                                f"k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', "
                                f"pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) "
                                f"ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y';",
                            ]
                        )

                    print("Adding data in tables...")
                    self.run_command(
                        [
                            "sysbench",
                            "/usr/share/sysbench/oltp_insert.lua",
                            f"--tables={self.num_tables}",
                            "--mysql-db=test",
                            "--mysql-user=root",
                            "--threads=50",
                            "--db-driver=mysql",
                            f"--mysql-socket={self.socket}",
                            "--time=30",
                        ],
                        check=False,
                        capture_output=True,
                    )

        finally:
            os.chdir(original_cwd)

    def incremental_backup(
        self,
        backup_params: str = "",
        prepare_params: str = "",
        restore_params: str = "",
        mysqld_options: str = "",
    ):
        """Perform incremental backup, prepare, and restore."""
        log_date = datetime.now().strftime("%d_%m_%Y_%M")
        backup_params_with_lock = f"{backup_params} --lock-ddl={self.lock_ddl}".strip()

        print("Taking full backup")
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        os.makedirs(os.path.join(self.backup_dir, "full"), exist_ok=True)

        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir, exist_ok=True)

        # Full backup
        full_backup_log = os.path.join(
            self.logdir, f"full_backup_{log_date}_log"
        )
        cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--user=root",
            "--password=",
            "--backup",
            f"--target-dir={os.path.join(self.backup_dir, 'full')}",
            f"-S{self.socket}",
            f"--datadir={self.datadir}",
        ]
        if backup_params_with_lock:
            cmd.extend(backup_params_with_lock.split())
        result = self.run_command(
            cmd,
            check=False,
            capture_output=True,
        )
        with open(full_backup_log, "w") as f:
            if result.stderr:
                f.write(result.stderr)
            if result.stdout:
                f.write(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"ERR: Full Backup failed. Please check the log at: {full_backup_log}"
            )
        print(
            f"Full backup was successfully created at: {os.path.join(self.backup_dir, 'full')}. "
            f"Logs available at: {full_backup_log}"
        )

        print("Adding data in database")
        # Start sysbench in background
        sysbench_process = subprocess.Popen(
            [
                "sysbench",
                "/usr/share/sysbench/oltp_insert.lua",
                f"--tables={self.num_tables}",
                "--mysql-db=test",
                "--mysql-user=root",
                "--threads=50",
                "--db-driver=mysql",
                f"--mysql-socket={self.socket}",
                "--time=20",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(5)

        print("Taking incremental backup")
        inc_backup_log = os.path.join(self.logdir, f"inc_backup_{log_date}_log")
        cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--user=root",
            "--password=",
            "--backup",
            f"--target-dir={os.path.join(self.backup_dir, 'inc')}",
            f"--incremental-basedir={os.path.join(self.backup_dir, 'full')}",
            f"-S{self.socket}",
            f"--datadir={self.datadir}",
        ]
        if backup_params_with_lock:
            cmd.extend(backup_params_with_lock.split())
        result = self.run_command(
            cmd,
            check=False,
            capture_output=True,
        )
        with open(inc_backup_log, "w") as f:
            if result.stderr:
                f.write(result.stderr)
            if result.stdout:
                f.write(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"ERR: Incremental Backup failed. Please check the log at: {inc_backup_log}"
            )
        print(
            f"Inc backup was successfully created at: {os.path.join(self.backup_dir, 'inc')}. "
            f"Logs available at: {inc_backup_log}"
        )

        # Wait for sysbench to finish
        sysbench_process.wait()

        print("Preparing full backup")
        prepare_full_log = os.path.join(
            self.logdir, f"prepare_full_backup_{log_date}_log"
        )
        cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--user=root",
            "--password=",
            "--prepare",
            "--apply-log-only",
            f"--target_dir={os.path.join(self.backup_dir, 'full')}",
        ]
        if prepare_params:
            cmd.extend(prepare_params.split())
        result = self.run_command(
            cmd,
            check=False,
            capture_output=True,
        )
        with open(prepare_full_log, "w") as f:
            if result.stderr:
                f.write(result.stderr)
            if result.stdout:
                f.write(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"ERR: Prepare of full backup failed. Please check the log at: {prepare_full_log}"
            )
        print(
            f"Prepare of full backup was successful. Logs available at: {prepare_full_log}"
        )

        print("Preparing incremental backup")
        prepare_inc_log = os.path.join(
            self.logdir, f"prepare_inc_backup_{log_date}_log"
        )
        cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--user=root",
            "--password=",
            "--prepare",
            f"--target_dir={os.path.join(self.backup_dir, 'full')}",
            f"--incremental-dir={os.path.join(self.backup_dir, 'inc')}",
        ]
        if prepare_params:
            cmd.extend(prepare_params.split())
        result = self.run_command(
            cmd,
            check=False,
            capture_output=True,
        )
        with open(prepare_inc_log, "w") as f:
            if result.stderr:
                f.write(result.stderr)
            if result.stdout:
                f.write(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"ERR: Prepare of incremental backup failed. Please check the log at: {prepare_inc_log}"
            )
        print(
            f"Prepare of incremental backup was successful. Logs available at: {prepare_inc_log}"
        )

        print("Restart mysql server to stop all running queries")
        self.run_command(
            [
                os.path.join(self.mysqldir, "bin", "mysqladmin"),
                "-uroot",
                f"-S{self.socket}",
                "shutdown",
            ]
        )
        time.sleep(2)

        original_cwd = os.getcwd()
        try:
            os.chdir(self.mysqldir)
            cmd = ["bash", "./start", "--log-bin=binlog"]
            if mysqld_options:
                cmd.extend(mysqld_options.split())
            self.run_command(
                cmd,
                check=False,
                capture_output=True,
            )
            if not self.wait_for_mysql():
                raise RuntimeError(
                    f"Database could not be started in location {self.mysqldir}. "
                    f"Database logs: {os.path.join(self.mysqldir, 'log')}"
                )
            print("The mysql server was restarted successfully")
        finally:
            os.chdir(original_cwd)

        print("Collecting current data of innodb tables")
        # Get record count and checksum for each table
        rc_innodb_orig = {}
        chk_innodb_orig = {}
        for i in range(1, self.num_tables + 1):
            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"SELECT COUNT(*) FROM test.sbtest{i};",
                ],
                capture_output=True,
            )
            rc_innodb_orig[i] = int(result.stdout.strip())

            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"CHECKSUM TABLE test.sbtest{i};",
                ],
                capture_output=True,
            )
            chk_innodb_orig[i] = int(result.stdout.split()[1])

        print("Stopping mysql server and moving data directory")
        self.run_command(
            [
                os.path.join(self.mysqldir, "bin", "mysqladmin"),
                "-uroot",
                f"-S{self.socket}",
                "shutdown",
            ]
        )
        data_orig_dir = os.path.join(
            self.mysqldir, f"data_orig_{datetime.now().strftime('%d_%m_%Y')}"
        )
        if os.path.exists(data_orig_dir):
            shutil.rmtree(data_orig_dir)
        shutil.move(self.datadir, data_orig_dir)

        print("Restoring full backup")
        res_backup_log = os.path.join(self.logdir, f"res_backup_{log_date}_log")
        cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--user=root",
            "--password=",
            "--copy-back",
            f"--target-dir={os.path.join(self.backup_dir, 'full')}",
            f"--datadir={self.datadir}",
        ]
        if restore_params:
            cmd.extend(restore_params.split())
        result = self.run_command(
            cmd,
            check=False,
            capture_output=True,
        )
        with open(res_backup_log, "w") as f:
            if result.stderr:
                f.write(result.stderr)
            if result.stdout:
                f.write(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"ERR: Restore of full backup failed. Please check the log at: {res_backup_log}"
            )
        print(
            f"Restore of full backup was successful. Logs available at: {res_backup_log}"
        )

        print("Starting mysql server")
        original_cwd = os.getcwd()
        try:
            os.chdir(self.mysqldir)
            cmd = ["bash", "./start", "--log-bin=binlog"]
            if mysqld_options:
                cmd.extend(mysqld_options.split())
            self.run_command(
                cmd,
                check=False,
                capture_output=True,
            )
            if not self.wait_for_mysql():
                raise RuntimeError(
                    f"Database could not be started in location {self.mysqldir}. "
                    "The restore was unsuccessful. "
                    f"Database logs: {os.path.join(self.mysqldir, 'log')}"
                )
            print("The mysql server was started successfully")
        finally:
            os.chdir(original_cwd)

        print("Check xtrabackup for binlog position")
        binlog_info_file = os.path.join(self.backup_dir, "full", "xtrabackup_binlog_info")
        with open(binlog_info_file, "r") as f:
            binlog_info = f.read().strip().split()
            xb_binlog_file = binlog_info[0]
            xb_binlog_pos = binlog_info[1]
        print(f"Xtrabackup binlog position: {xb_binlog_file}, {xb_binlog_pos}")

        print(
            f"Applying binlog to restored data starting from {xb_binlog_file}, {xb_binlog_pos}"
        )
        binlog_file_path = os.path.join(data_orig_dir, xb_binlog_file)
        if os.path.exists(binlog_file_path):
            mysqlbinlog_result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysqlbinlog"),
                    binlog_file_path,
                    f"--start-position={xb_binlog_pos}",
                ],
                capture_output=True,
                check=False,
            )
            if mysqlbinlog_result.returncode == 0 and mysqlbinlog_result.stdout:
                mysql_result = self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                    ],
                    input=mysqlbinlog_result.stdout,
                    check=False,
                )
                if mysql_result.returncode != 0:
                    print("ERR: The binlog could not be applied to the restored data")
            else:
                print("ERR: The binlog could not be applied to the restored data")
        else:
            print(f"WARN: Binlog file not found: {binlog_file_path}")

        time.sleep(5)
        print("Checking restored data")
        print("Check the table status")
        check_err = 0
        for i in range(1, self.num_tables + 1):
            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"CHECK TABLE test.sbtest{i}",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                print(f"ERR: CHECK TABLE test.sbtest{i} query failed")
                if not self.check_mysql_running():
                    raise RuntimeError(
                        f"ERR: The database has gone down due to corruption in table test.sbtest{i}"
                    )
                check_err = 1
            else:
                table_status = result.stdout.strip().split()[-1]
                if table_status != "OK":
                    print(
                        f"ERR: CHECK TABLE test.sbtest{i} query displayed the table status as '{table_status}'"
                    )
                    check_err = 1

        if check_err == 0:
            print("All innodb tables status: OK")
        else:
            print("After restore, some tables may be corrupt, check table status is not OK")

        print("Check the record count of tables in database test")
        rc_err = 0
        checksum_err = 0
        for i in range(1, self.num_tables + 1):
            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"SELECT COUNT(*) FROM test.sbtest{i};",
                ],
                capture_output=True,
            )
            rc_innodb_res = int(result.stdout.strip())
            if rc_innodb_orig[i] != rc_innodb_res:
                print(
                    f"ERR: The record count of test.sbtest{i} changed after restore. "
                    f"Record count in original data: {rc_innodb_orig[i]}. "
                    f"Record count in restored data: {rc_innodb_res}."
                )
                rc_err = 1

        if rc_err == 0:
            print("Match record count of tables in database test with original data: Pass")

        print("Check the checksum of each table in database test")
        for i in range(1, self.num_tables + 1):
            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"CHECKSUM TABLE test.sbtest{i};",
                ],
                capture_output=True,
            )
            chk_innodb_res = int(result.stdout.split()[1])
            if chk_innodb_orig[i] != chk_innodb_res:
                print(
                    f"ERR: The checksum of test.sbtest{i} changed after restore. "
                    f"Checksum in original data: {chk_innodb_orig[i]}. "
                    f"Checksum in restored data: {chk_innodb_res}."
                )
                checksum_err = 1

        if checksum_err == 0:
            print("Match checksum of all tables in database test with original data: Pass")

        print("Check for gaps in primary sequence id of tables")
        gap_found = 0
        for i in range(1, self.num_tables + 1):
            result = self.run_command(
                [
                    os.path.join(self.mysqldir, "bin", "mysql"),
                    "-uroot",
                    f"-S{self.socket}",
                    "-Bse",
                    f"SELECT id FROM test.sbtest{i} ORDER BY id ASC",
                ],
                capture_output=True,
            )
            ids = [int(line) for line in result.stdout.strip().split("\n") if line]
            for j, id_val in enumerate(ids, start=1):
                if id_val != j:
                    print(
                        f"ERR: Gap found in test.sbtest{i}. "
                        f"Expected sequence number for ID is: {j}. "
                        f"Actual sequence number for ID is: {id_val}."
                    )
                    gap_found = 1
                    break

        if gap_found == 0:
            print("No gaps found in primary sequence id of tables: Pass")

    def change_storage_engine(self):
        """Change the storage engine of a table."""
        print("Change the storage engine of test.sbtest1 to MYISAM, INNODB continuously")
        import threading

        def change_engine():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "alter table test.sbtest1 ENGINE=MYISAM;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "alter table test.sbtest1 ENGINE=INNODB;",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=change_engine, daemon=True)
        thread.start()
        return thread

    def add_drop_index(self):
        """Add and drop an index in a table."""
        print("Add and drop an index in the test.sbtest1 table")
        import threading

        def add_drop():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "CREATE INDEX kc on test.sbtest1 (k,c);",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "DROP INDEX kc on test.sbtest1;",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=add_drop, daemon=True)
        thread.start()
        return thread

    def add_drop_tablespace(self):
        """Add a table to a tablespace and then drop the table, tablespace."""
        print("Add an innodb table to a tablespace and drop the table, tablespace")
        import threading

        def add_drop_ts():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "CREATE TABLESPACE ts1 ADD DATAFILE 'ts1.ibd' Engine=InnoDB;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "CREATE TABLE test.sbtest1copy SELECT * from test.sbtest1;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest1copy TABLESPACE ts1;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "DROP TABLE test.sbtest1copy;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "DROP TABLESPACE ts1;",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=add_drop_ts, daemon=True)
        thread.start()
        return thread

    def change_compression(self):
        """Change the compression of a table."""
        print("Change the compression of an innodb table")
        import threading

        def change_comp():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest1 compression='lz4';",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest1 compression='zlib';",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest1 compression='';",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=change_comp, daemon=True)
        thread.start()
        return thread

    def change_row_format(self):
        """Change the row format of a table."""
        print("Change the row format of an innodb table")
        import threading

        def change_row():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest2 ROW_FORMAT=COMPRESSED;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest2 ROW_FORMAT=DYNAMIC;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest2 ROW_FORMAT=COMPACT;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "ALTER TABLE test.sbtest2 ROW_FORMAT=REDUNDANT;",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=change_row, daemon=True)
        thread.start()
        return thread

    def update_truncate_table(self):
        """Update data in tables and then truncate it."""
        print("Update an innodb table and then truncate it")
        import threading

        def update_truncate():
            for _ in range(10):
                if not self.check_mysql_running():
                    break
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "SET @@SESSION.OPTIMIZER_SWITCH='firstmatch=ON';",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "UPDATE test.sbtest1 SET c='Œ„´‰?Á¨ˆØ?''';",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "OPTIMIZE TABLE test.sbtest1;",
                    ],
                    check=False,
                    capture_output=True,
                )
                self.run_command(
                    [
                        os.path.join(self.mysqldir, "bin", "mysql"),
                        "-uroot",
                        f"-S{self.socket}",
                        "-e",
                        "TRUNCATE test.sbtest1;",
                    ],
                    check=False,
                    capture_output=True,
                )

        thread = threading.Thread(target=update_truncate, daemon=True)
        thread.start()
        return thread


# Pytest fixtures and tests
@pytest.fixture(scope="function")
def backup_helper():
    """Create a backup test helper instance."""
    helper = InnoDBBackupTestHelper()
    yield helper
    # Cleanup if needed


def test_inc_backup(backup_helper):
    """Test: Incremental Backup and Restore"""
    print("Running Tests")
    print("Test: Incremental Backup and Restore")

    backup_helper.initialize_db()
    backup_helper.incremental_backup()


def test_chg_storage_eng(backup_helper):
    """Test: Backup and Restore during change in storage engine"""
    print("Test: Backup and Restore during change in storage engine")

    backup_helper.initialize_db()
    backup_helper.change_storage_engine()
    backup_helper.incremental_backup()


def test_add_drop_index(backup_helper):
    """Test: Backup and Restore during add and drop index"""
    print("Test: Backup and Restore during add and drop index")

    backup_helper.initialize_db()
    backup_helper.add_drop_index()
    backup_helper.incremental_backup()


def test_add_drop_tablespace(backup_helper):
    """Test: Backup and Restore during add and drop tablespace"""
    print("Test: Backup and Restore during add and drop tablespace")

    backup_helper.initialize_db()
    backup_helper.add_drop_tablespace()
    backup_helper.incremental_backup()


def test_change_compression(backup_helper):
    """Test: Backup and Restore during change in compression"""
    print("Test: Backup and Restore during change in compression")

    backup_helper.initialize_db()
    backup_helper.change_compression()
    backup_helper.incremental_backup()


def test_change_row_format(backup_helper):
    """Test: Backup and Restore during change in row format"""
    print("Test: Backup and Restore during change in row format")

    backup_helper.initialize_db()
    backup_helper.change_row_format()
    backup_helper.incremental_backup()


def test_update_truncate_table(backup_helper):
    """Test: Backup and Restore during update and truncate of a table"""
    print("Test: Backup and Restore during update and truncate of a table")

    backup_helper.initialize_db()
    backup_helper.update_truncate_table()
    backup_helper.incremental_backup()


def test_run_all_statements(backup_helper):
    """Test suite runs the statements for all previous tests simultaneously in background"""
    backup_helper.initialize_db()

    backup_helper.change_storage_engine()
    backup_helper.add_drop_index()
    backup_helper.add_drop_tablespace()
    backup_helper.change_compression()
    backup_helper.change_row_format()
    backup_helper.update_truncate_table()

    backup_helper.incremental_backup()


def test_inc_backup_encryption(backup_helper):
    """Test: Incremental Backup and Restore for PS with encryption"""
    print("Test: Incremental Backup and Restore for PS with encryption")

    # Note: Binlog cannot be applied to backup if it is encrypted

    # For PS debug build
    mysqld_options = (
        "--early-plugin-load=keyring_file.so "
        "--keyring_file_data={}/keyring "
        "--innodb-undo-log-encrypt "
        "--innodb-redo-log-encrypt "
        "--log-slave-updates "
        "--gtid-mode=ON "
        "--enforce-gtid-consistency "
        "--binlog-format=row "
        "--master_verify_checksum=ON "
        "--binlog_checksum=CRC32"
    ).format(backup_helper.mysqldir)

    backup_helper.initialize_db(mysqld_options)

    backup_params = (
        "--keyring_file_data={}/keyring "
        "--xtrabackup-plugin-dir={}/../lib/plugin"
    ).format(backup_helper.mysqldir, backup_helper.xtrabackup_dir)

    prepare_params = backup_params
    restore_params = backup_params

    backup_helper.incremental_backup(
        backup_params=backup_params,
        prepare_params=prepare_params,
        restore_params=restore_params,
        mysqld_options=mysqld_options,
    )


if __name__ == "__main__":
    # Allow running specific tests
    pytest.main([__file__, "-v"])
