#!/usr/bin/env python3
"""
This script tests backup for innodb and myrocks tables
Assumption: PS and PXB are already installed as tarballs
Usage:
1. Set paths in this script:
   xtrabackup_dir, backup_dir, mysqldir, datadir, qascripts, logdir,
   vault_config, cloud_config
2. Set config variables in the script for
   sysbench, stream, encryption key, kmip, kms
3. For usage run the script as: pytest innodb_myrocks_backup_tests.py
4. Logs are available in: logdir
"""

import os
import sys
import subprocess
import shutil
import time
import re
import signal
import threading
from datetime import datetime
from typing import Optional, List, Tuple
import psutil
import pytest

# Set script variables
HOME = os.path.expanduser("~")
XTRABACKUP_DIR = os.path.join(HOME, "pxb-9.1/bld_9.1/install/bin")
MYSQLDIR = os.path.join(HOME, "mysql-9.1/bld_9.1/install")
DATADIR = os.path.join(MYSQLDIR, "data")
BACKUP_DIR = os.path.join(HOME, f"dbbackup_{datetime.now().strftime('%d_%m_%Y')}")
QASCRIPTS = os.path.join(HOME, "server-qa")
LOGDIR = os.path.join(HOME, "backuplogs")
CLOUD_CONFIG = os.path.join(HOME, "aws.cnf")  # Only required for cloud backup tests
MYSQL_START_TIMEOUT = 60

# RocksDB and server settings
ROCKSDB = "disabled"  # Set to "enabled" for MyRocks tests
SERVER_TYPE = "PS"  # Default server PS
INSTALL_TYPE = "tarball"  # Set to "tarball" or "package"
LOCK_DDL = "on"  # Accepted values: on, reduced

# Sysbench variables
NUM_TABLES = 10
TABLE_SIZE = 1000
RANDOM_TYPE = "uniform"

# Stream and encryption key
BACKUP_STREAM = "backup.xbstream"
ENCRYPT_KEY = "mHU3Zs5sRcSB7zBAJP1BInPP5lgShKly"
BACKUP_TAR = "backup.tar"

# Set user for backup
BACKUP_USER = "root"

# KMIP configuration
KMIP_SERVER_ADDRESS = "0.0.0.0"
KMIP_SERVER_PORT = 5696
KMIP_CLIENT_CA = "/home/manish.chawla/.local/etc/pykmip/client_certificate_john_smith.pem"
KMIP_CLIENT_KEY = "/home/manish.chawla/.local/etc/pykmip/client_key_john_smith.pem"
KMIP_SERVER_CA = "/home/manish.chawla/.local/etc/pykmip/server_certificate.pem"

# KMS configuration
KMS_REGION = os.environ.get("KMS_REGION", "us-east-1")
KMS_ID = os.environ.get("KMS_KEYID", "")
KMS_AUTH_KEY = os.environ.get("KMS_AUTH_KEY", "")
KMS_SECRET_KEY = os.environ.get("KMS_SECRET_KEY", "")


class BackupTestHelper:
    """Helper class for backup tests."""

    def __init__(
        self,
        xtrabackup_dir: str = XTRABACKUP_DIR,
        mysqldir: str = MYSQLDIR,
        datadir: str = DATADIR,
        backup_dir: str = BACKUP_DIR,
        qascripts: str = QASCRIPTS,
        logdir: str = LOGDIR,
        cloud_config: str = CLOUD_CONFIG,
        rocksdb: str = ROCKSDB,
        server_type: str = SERVER_TYPE,
        install_type: str = INSTALL_TYPE,
        lock_ddl: str = LOCK_DDL,
        num_tables: int = NUM_TABLES,
        table_size: int = TABLE_SIZE,
        random_type: str = RANDOM_TYPE,
        backup_stream: str = BACKUP_STREAM,
        encrypt_key: str = ENCRYPT_KEY,
        backup_tar: str = BACKUP_TAR,
        backup_user: str = BACKUP_USER,
    ):
        """Initialize test helper with configuration."""
        self.xtrabackup_dir = xtrabackup_dir
        self.mysqldir = mysqldir
        self.datadir = datadir
        self.backup_dir = backup_dir
        self.qascripts = qascripts
        self.logdir = logdir
        self.cloud_config = cloud_config
        self.rocksdb = rocksdb
        self.server_type = server_type
        self.install_type = install_type
        self.lock_ddl = lock_ddl
        self.num_tables = num_tables
        self.table_size = table_size
        self.random_type = random_type
        self.backup_stream = backup_stream
        self.encrypt_key = encrypt_key
        self.backup_tar = backup_tar
        self.backup_user = backup_user
        self.mysql_start_timeout = MYSQL_START_TIMEOUT

        # Runtime variables
        self.version: Optional[str] = None
        self.version_normalized: Optional[int] = None
        self.pxb_version: Optional[str] = None
        self.pxb_version_normalized: Optional[int] = None
        self.mysql_pid: Optional[int] = None
        self.mysqld_options: str = ""
        self.backup_params: str = ""
        self.prepare_params: str = ""
        self.restore_params: str = ""
        self.keyring_file: Optional[str] = None

        # Vault configuration
        self.vault_config: Optional[str] = None
        self.vault_url: Optional[str] = None
        self.secret_mount_point: Optional[str] = None
        self.token: Optional[str] = None
        self.vault_ca: Optional[str] = None

        # Initialize paths
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}:{self.xtrabackup_dir}"

    @staticmethod
    def normalize_version(version_str: str) -> int:
        """Normalize version string to integer for comparison."""
        major = 0
        minor = 0
        patch = 0

        match = re.match(r"^(\d+)\.(\d+)\.?(\d*)([\.0-9])*$", version_str)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0

        return int(f"{major:02d}{minor:02d}{patch:02d}")

    def get_mysql_version(self) -> Tuple[str, int]:
        """Get MySQL version and normalized version."""
        try:
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysqld"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            version_match = re.search(r"Ver\s+([0-9]+\.[0-9]+[\.0-9]*)", result.stdout)
            if version_match:
                ver = version_match.group(1)
                normalized = self.normalize_version(ver)
                return ver, normalized
        except Exception as e:
            print(f"Error getting MySQL version: {e}")
        return "0.0.0", 0

    def get_pxb_version(self) -> Tuple[str, int]:
        """Get Percona XtraBackup version and normalized version."""
        try:
            result = subprocess.run(
                [os.path.join(self.xtrabackup_dir, "xtrabackup"), "--no-defaults", "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            version_match = re.search(
                r"version\s+([0-9]+\.[0-9]+[\.0-9]*)", result.stderr or result.stdout
            )
            if version_match:
                ver = version_match.group(1)
                normalized = self.normalize_version(ver)
                return ver, normalized
        except Exception as e:
            print(f"Error getting PXB version: {e}")
        return "0.0.0", 0

    def find_server_type(self) -> str:
        """Determine if server is PS or MS."""
        try:
            result = subprocess.run(
                [os.path.join(self.mysqldir, "bin/mysqld"), "--help", "--verbose"],
                capture_output=True,
                text=True,
                check=True,
            )
            if "innodb-sys-tablespace-encrypt" in result.stdout:
                self.server_type = "PS"
            else:
                self.server_type = "MS"
        except Exception:
            self.server_type = "PS"
        return self.server_type

    def run_command(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = True,
        log_file: Optional[str] = None,
        background: bool = False,
        stdin_file: Optional[str] = None,
        stdout_file: Optional[str] = None,
    ):
        """Run a shell command."""
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"Command: {' '.join(cmd)}\n")
                f.write(f"Time: {datetime.now()}\n")

        if background:
            stdout_handle = (
                open(stdout_file, "wb") if stdout_file else subprocess.DEVNULL
            )
            stderr_handle = (
                open(log_file, "a", encoding="utf-8") if log_file else subprocess.DEVNULL
            )
            process = subprocess.Popen(
                cmd,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            return process
        else:
            stdin_handle = open(stdin_file, "rb") if stdin_file else None
            stdout_handle = open(stdout_file, "wb") if stdout_file else None
            stderr_handle = (
                open(log_file, "w", encoding="utf-8") if log_file else subprocess.PIPE
            )

            result = subprocess.run(
                cmd,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                capture_output=capture_output and not stdout_file,
                text=not stdout_file,
                check=check,
            )

            if stdin_handle:
                stdin_handle.close()
            if stdout_handle:
                stdout_handle.close()
            if stderr_handle and log_file and not stdout_file:
                stderr_handle.close()

            if log_file and result.stdout and not stdout_file:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(result.stdout)

            return result

    def start_vault_server(self):
        """Start vault server."""
        print("Setting up vault server")
        vault_dir = os.path.join(HOME, "vault")
        if not os.path.exists(vault_dir):
            os.makedirs(vault_dir)

        # Clean vault directory
        for item in os.listdir(vault_dir):
            item_path = os.path.join(vault_dir, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)

        # Kill any previously running vault server
        subprocess.run(["killall", "vault"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        # Start vault server
        vault_setup_script = os.path.join(self.qascripts, "vault_test_setup.sh")
        subprocess.run(
            [vault_setup_script, f"--workdir={vault_dir}", "--use-ssl"],
            capture_output=True,
            stderr=subprocess.DEVNULL,
        )

        self.vault_config = os.path.join(vault_dir, "keyring_vault_ps.cnf")
        if os.path.exists(self.vault_config):
            with open(self.vault_config, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("vault_url"):
                        self.vault_url = line.split("=", 1)[1].strip()
                    elif line.startswith("secret_mount_point"):
                        self.secret_mount_point = line.split("=", 1)[1].strip()
                    elif line.startswith("token"):
                        self.token = line.split("=", 1)[1].strip()
                    elif line.startswith("vault_ca"):
                        self.vault_ca = line.split("=", 1)[1].strip()

    def start_server(self, add_options: str = ""):
        """Start MySQL server."""
        # Kill existing mysqld processes
        subprocess.run(["pkill", "-9", "mysqld"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        time.sleep(1)

        cmd = [
            "rr",
            os.path.join(self.mysqldir, "bin/mysqld"),
            "--no-defaults",
            f"--basedir={self.mysqldir}",
            f"--datadir={self.datadir}",
        ]

        # Add MYSQLD_OPTIONS if set
        if self.mysqld_options:
            cmd.extend(self.mysqld_options.split())

        # Add additional options
        if add_options:
            cmd.extend(add_options.split())

        cmd.extend([
            "--port=21000",
            f"--socket={self.mysqldir}/socket.sock",
            f"--plugin-dir={self.mysqldir}/lib/plugin",
            "--max-connections=1024",
            f"--log-error={self.datadir}/error.log",
            "--general-log",
            "--log-error-verbosity=3",
            "--core-file",
        ])

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
                        f"-S{self.mysqldir}/socket.sock",
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
                    f"ERR: Database could not be started. Please check error logs: {self.datadir}/error.log"
                )

    def mysql_execute(self, query: str, database: Optional[str] = None, batch_mode: bool = False) -> Optional[str]:
        """Execute MySQL query and return result.
        
        Args:
            query: SQL query to execute
            database: Optional database name
            batch_mode: If True, use -Bse flags to get only values (no headers)
        """
        cmd = [
            os.path.join(self.mysqldir, "bin/mysql"),
            "-uroot",
            f"-S{self.mysqldir}/socket.sock",
        ]
        if batch_mode:
            cmd.append("-Bse")
            if database:
                cmd.append(database)
            cmd.append(query)
        else:
            if database:
                cmd.append(database)
            cmd.extend(["-e", query])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def initialize_db(self, mysqld_options: str = ""):
        """Initialize and start MySQL database."""
        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir)

        print("=>Creating data directory")
        if os.path.exists(self.datadir):
            shutil.rmtree(self.datadir)

        log_file = os.path.join(self.mysqldir, "mysql_install_db.log")
        with open(log_file, "w", encoding="utf-8") as f:
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

        print("=>Starting MySQL server")
        self.mysqld_options = mysqld_options
        self.start_server()

        print("Creating innodb data in database")
        if not shutil.which("sysbench"):
            pytest.fail("ERR: Sysbench not found, data could not be created")

        self.mysql_execute("CREATE DATABASE test")

        if "keyring" not in mysqld_options:
            # Create tables without encryption
            subprocess.run(
                [
                    "sysbench",
                    "/usr/share/sysbench/oltp_insert.lua",
                    f"--tables={self.num_tables}",
                    f"--table-size={self.table_size}",
                    "--mysql-db=test",
                    "--mysql-user=root",
                    "--threads=100",
                    "--db-driver=mysql",
                    f"--mysql-socket={self.mysqldir}/socket.sock",
                    f"--rand-type={self.random_type}",
                    "prepare",
                ],
                check=True,
            )

            if self.rocksdb == "enabled":
                print("Installing rocksdb storage engine")
                myrocks_sql = os.path.join(self.qascripts, "MyRocks.sql")
                if os.path.exists(myrocks_sql):
                    with open(myrocks_sql, "r", encoding="utf-8") as f:
                        self.mysql_execute(f.read())

                print("Creating rocksdb data in database")
                self.mysql_execute("CREATE DATABASE IF NOT EXISTS test_rocksdb")
                subprocess.run(
                    [
                        "sysbench",
                        "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}",
                        f"--table-size={self.table_size}",
                        "--mysql-db=test_rocksdb",
                        "--mysql-user=root",
                        "--threads=100",
                        "--db-driver=mysql",
                        "--mysql-storage-engine=ROCKSDB",
                        f"--mysql-socket={self.mysqldir}/socket.sock",
                        f"--rand-type={self.random_type}",
                        "prepare",
                    ],
                    check=True,
                )
        else:
            # Create encrypted tables
            print("Creating encrypted tables in innodb")
            result = subprocess.run(
                [
                    "sysbench",
                    "/usr/share/sysbench/oltp_insert.lua",
                    f"--tables={self.num_tables}",
                    f"--table-size={self.table_size}",
                    "--mysql-db=test",
                    "--mysql-user=root",
                    "--threads=100",
                    "--db-driver=mysql",
                    f"--mysql-socket={self.mysqldir}/socket.sock",
                    "--mysql-table-options=Encryption='Y'",
                    f"--rand-type={self.random_type}",
                    "prepare",
                ],
                capture_output=True,
                stderr=subprocess.DEVNULL,
                check=False,
            )

            if result.returncode != 0:
                # Create tables manually
                for i in range(1, self.num_tables + 1):
                    print(f"Creating the table sbtest{i}...")
                    self.mysql_execute(
                        f"CREATE TABLE test.sbtest{i} (id int(11) NOT NULL AUTO_INCREMENT, "
                        f"k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', "
                        f"pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) "
                        f"ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y';"
                    )

                print("Adding data in tables...")
                subprocess.run(
                    [
                        "sysbench",
                        "/usr/share/sysbench/oltp_insert.lua",
                        f"--tables={self.num_tables}",
                        "--mysql-db=test",
                        "--mysql-user=root",
                        "--threads=50",
                        "--db-driver=mysql",
                        f"--mysql-socket={self.mysqldir}/socket.sock",
                        "--time=30",
                        f"--rand-type={self.random_type}",
                        "run",
                    ],
                    capture_output=True,
                    stderr=subprocess.DEVNULL,
                )

    def process_backup(self, backup_type: str, backup_params: str, ext_dir: str):
        """Extract, decrypt and decompress backup."""
        log_date = datetime.now().strftime("%d_%m_%Y_%M")

        if backup_type == "stream":
            backup_stream_path = os.path.join(self.backup_dir, self.backup_stream)
            if not os.path.exists(backup_stream_path):
                pytest.fail(
                    f"ERR: The backup stream file was not created in {backup_stream_path}"
                )

            print(f"Extract the backup from the stream file at {backup_stream_path}")
            extract_log = os.path.join(self.logdir, f"extract_backup_{log_date}_log")
            with open(backup_stream_path, "rb") as stream_file, open(
                extract_log, "a", encoding="utf-8"
            ) as log_file:
                result = subprocess.run(
                    [
                        os.path.join(self.xtrabackup_dir, "xbstream"),
                        f"--directory={ext_dir}",
                        "--extract",
                        "--verbose",
                    ],
                    stdin=stream_file,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            print(f"Backup was successfully extracted. Logs available at: {extract_log}")

        if "--encrypt-key" in backup_params:
            print(f"Decrypting the backup files at {ext_dir}")
            decrypt_log = os.path.join(self.logdir, f"decrypt_backup_{log_date}_log")
            with open(decrypt_log, "a", encoding="utf-8") as log_file:
                subprocess.run(
                    [
                        os.path.join(self.xtrabackup_dir, "xtrabackup"),
                        "--decrypt=AES256",
                        f"--encrypt-key={self.encrypt_key}",
                        f"--target-dir={ext_dir}",
                        "--parallel=10",
                    ],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            print(f"Backup was successfully decrypted. Logs available at: {decrypt_log}")

        if "--compress" in backup_params:
            if not shutil.which("qpress"):
                pytest.fail(
                    "ERR: The qpress package is not installed. It is required to decompress the backup."
                )

            print(f"Decompressing the backup files at {ext_dir}")
            decompress_log = os.path.join(self.logdir, f"decompress_backup_{log_date}_log")
            with open(decompress_log, "a", encoding="utf-8") as log_file:
                subprocess.run(
                    [
                        os.path.join(self.xtrabackup_dir, "xtrabackup"),
                        "--decompress",
                        "--parallel=10",
                        f"--target-dir={ext_dir}",
                    ],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            print(f"Backup was successfully decompressed. Logs available at: {decompress_log}")

    def incremental_backup(
        self,
        backup_params: str = "",
        prepare_params: str = "",
        restore_params: str = "",
        mysqld_options: str = "",
        backup_type: str = "",
        cloud_params: str = "",
    ):
        """Take incremental backup and restore."""
        log_date = datetime.now().strftime("%d_%m_%Y_%M")

        # Prepare backup parameters
        full_backup_params = f"{backup_params} --lock-ddl={self.lock_ddl}"

        # Clean and create backup directory
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        os.makedirs(os.path.join(self.backup_dir, "full"), exist_ok=True)
        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir)

        # Take full backup
        full_backup_log = os.path.join(self.logdir, f"full_backup_{log_date}_log")
        xtrabackup_cmd = [
            "rr",
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--no-defaults",
            f"--user={self.backup_user}",
            "--password=",
            "--backup",
            f"--target-dir={os.path.join(self.backup_dir, 'full')}",
            f"-S{self.mysqldir}/socket.sock",
            f"--datadir={self.datadir}",
        ]

        # Add backup parameters
        if full_backup_params:
            xtrabackup_cmd.extend(full_backup_params.split())

        if backup_type == "cloud":
            print("=>Taking full backup and uploading it")
            xtrabackup_cmd.extend(["--extra-lsndir", self.backup_dir])
            xtrabackup_cmd.extend(["--stream=xbstream"])

            upload_log = os.path.join(self.logdir, f"upload_full_backup_{log_date}_log")
            with open(full_backup_log, "w", encoding="utf-8") as log_file, open(
                upload_log, "w", encoding="utf-8"
            ) as upload_file:
                xtrabackup_process = subprocess.Popen(
                    xtrabackup_cmd,
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                )
                xbcloud_cmd = [os.path.join(self.xtrabackup_dir, "xbcloud")]
                if cloud_params:
                    xbcloud_cmd.extend(cloud_params.split())
                xbcloud_cmd.extend(["put", f"full_backup_{log_date}"])
                xbcloud_process = subprocess.Popen(
                    xbcloud_cmd,
                    stdin=xtrabackup_process.stdout,
                    stdout=upload_file,
                    stderr=subprocess.STDOUT,
                )
                xtrabackup_process.stdout.close()
                xbcloud_process.wait()
                xtrabackup_process.wait()
        elif backup_type == "stream":
            print("=>Taking full backup and creating a stream file")
            xtrabackup_cmd.extend(["--stream=xbstream", "--parallel=10"])
            backup_stream_path = os.path.join(self.backup_dir, self.backup_stream)
            with open(full_backup_log, "w", encoding="utf-8") as log_file, open(
                backup_stream_path, "wb"
            ) as stream_file:
                subprocess.run(
                    xtrabackup_cmd,
                    stdout=stream_file,
                    stderr=log_file,
                    check=True,
                )
        elif backup_type == "tar":
            print("=>Taking full backup and creating a tar file")
            xtrabackup_cmd.extend(["--stream=tar"])
            backup_tar_path = os.path.join(self.backup_dir, self.backup_tar)
            with open(full_backup_log, "w", encoding="utf-8") as log_file, open(
                backup_tar_path, "wb"
            ) as tar_file:
                subprocess.run(
                    xtrabackup_cmd,
                    stdout=tar_file,
                    stderr=log_file,
                    check=True,
                )
        else:
            print("=>Taking full backup")
            xtrabackup_cmd.append("--register-redo-log-consumer")
            result = None
            with open(full_backup_log, "w", encoding="utf-8") as log_file:
                result = subprocess.run(
                    xtrabackup_cmd,
                    stderr=log_file,
                    check=False,
                )

        # Check backup result
        if backup_type != "cloud":
            with open(full_backup_log, "r", encoding="utf-8") as f:
                log_content = f.read()
                if (
                    "PXB will not be able to make a consistent backup" in log_content
                    or "PXB will not be able to take a consistent backup" in log_content
                ):
                    return  # Backup could not be completed due to DDL
                elif result and result.returncode != 0:
                    pytest.fail(
                        f"ERR: Full Backup failed. Please check the log at: {full_backup_log}"
                    )

        print(
            f"..Full backup was successfully created at: {os.path.join(self.backup_dir, 'full')}. "
            f"Logs available at: {full_backup_log}"
        )

        # Extract tar backup if needed
        if backup_type == "tar":
            backup_tar_path = os.path.join(self.backup_dir, self.backup_tar)
            if not os.path.exists(backup_tar_path):
                pytest.fail(
                    f"ERR: The backup tar file was not created in {backup_tar_path}"
                )

            print(f"Extract the backup from the tar file at {backup_tar_path}")
            extract_log = os.path.join(self.logdir, f"extract_backup_{log_date}_log")
            with open(extract_log, "w", encoding="utf-8") as log_file:
                subprocess.run(
                    ["tar", "-xvf", backup_tar_path, "-C", os.path.join(self.backup_dir, "full")],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            print(f"Backup was successfully extracted. Logs available at: {extract_log}")

        # Download cloud backup if needed
        if backup_type == "cloud":
            print("=>Downloading full backup")
            download_log = os.path.join(self.logdir, f"download_full_backup_{log_date}_log")
            download_stream_log = os.path.join(
                self.logdir, f"download_stream_full_backup_{log_date}_log"
            )

            xbcloud_cmd = [os.path.join(self.xtrabackup_dir, "xbcloud")]
            if cloud_params:
                xbcloud_cmd.extend(cloud_params.split())
            xbcloud_cmd.extend(["get", f"full_backup_{log_date}"])

            with open(download_log, "w", encoding="utf-8") as log_file, open(
                download_stream_log, "w", encoding="utf-8"
            ) as stream_log:
                xbcloud_process = subprocess.Popen(
                    xbcloud_cmd,
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                )
                xbstream_cmd = [
                    os.path.join(self.xtrabackup_dir, "xbstream"),
                    "-xv",
                    f"-C{os.path.join(self.backup_dir, 'full')}",
                ]
                subprocess.run(
                    xbstream_cmd,
                    stdin=xbcloud_process.stdout,
                    stdout=stream_log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
                xbcloud_process.wait()
            print(f"..Full backup was successfully downloaded at: {os.path.join(self.backup_dir, 'full')}")

        # Process backup (extract, decrypt, decompress)
        self.process_backup(backup_type, full_backup_params, os.path.join(self.backup_dir, "full"))

        # Add data in database
        print("=>Adding data in database")
        sysbench_cmd = [
            "sysbench",
            "/usr/share/sysbench/oltp_insert.lua",
            f"--tables={self.num_tables}",
            "--mysql-db=test",
            "--mysql-user=root",
            "--threads=50",
            "--db-driver=mysql",
            f"--mysql-socket={self.mysqldir}/socket.sock",
            "--time=20",
            f"--rand-type={self.random_type}",
            "run",
        ]
        subprocess.Popen(sysbench_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if self.rocksdb == "enabled":
            sysbench_cmd_rocks = sysbench_cmd.copy()
            sysbench_cmd_rocks[3] = "--mysql-db=test_rocksdb"
            sysbench_cmd_rocks.insert(-1, "--mysql-storage-engine=ROCKSDB")
            subprocess.Popen(sysbench_cmd_rocks, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(10)

        # Take incremental backup
        inc_backup_log = os.path.join(self.logdir, f"inc_backup_{log_date}_log")
        os.makedirs(os.path.join(self.backup_dir, "inc"), exist_ok=True)

        inc_xtrabackup_cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--no-defaults",
            f"--user={self.backup_user}",
            "--password=",
            "--backup",
            f"--target-dir={os.path.join(self.backup_dir, 'inc')}",
            f"--incremental-basedir={os.path.join(self.backup_dir, 'full')}",
            f"-S{self.mysqldir}/socket.sock",
            f"--datadir={self.datadir}",
        ]

        if full_backup_params:
            inc_xtrabackup_cmd.extend(full_backup_params.split())

        if backup_type == "cloud":
            print("=>Taking incremental backup and uploading it")
            inc_xtrabackup_cmd.extend(["--stream=xbstream"])
            upload_inc_log = os.path.join(self.logdir, f"upload_inc_backup_{log_date}_log")
            with open(inc_backup_log, "w", encoding="utf-8") as log_file, open(
                upload_inc_log, "w", encoding="utf-8"
            ) as upload_file:
                inc_xtrabackup_process = subprocess.Popen(
                    inc_xtrabackup_cmd,
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                )
                xbcloud_inc_cmd = [os.path.join(self.xtrabackup_dir, "xbcloud")]
                if cloud_params:
                    xbcloud_inc_cmd.extend(cloud_params.split())
                xbcloud_inc_cmd.extend(["put", f"inc_backup_{log_date}"])
                xbcloud_inc_process = subprocess.Popen(
                    xbcloud_inc_cmd,
                    stdin=inc_xtrabackup_process.stdout,
                    stdout=upload_file,
                    stderr=subprocess.STDOUT,
                )
                inc_xtrabackup_process.stdout.close()
                xbcloud_inc_process.wait()
                inc_xtrabackup_process.wait()
        elif backup_type == "stream":
            print("=>Taking incremental backup and creating a stream file")
            inc_xtrabackup_cmd.extend(["--stream=xbstream", "--parallel=10"])
            backup_stream_path = os.path.join(self.backup_dir, self.backup_stream)
            with open(inc_backup_log, "w", encoding="utf-8") as log_file, open(
                backup_stream_path, "wb"
            ) as stream_file:
                subprocess.run(
                    inc_xtrabackup_cmd,
                    stdout=stream_file,
                    stderr=log_file,
                    check=True,
                )
        else:
            print("=>Taking incremental backup")
            inc_xtrabackup_cmd.append("--register-redo-log-consumer")
            result = None
            with open(inc_backup_log, "w", encoding="utf-8") as log_file:
                result = subprocess.run(
                    inc_xtrabackup_cmd,
                    stderr=log_file,
                    check=False,
                )

        # Check incremental backup result
        if backup_type != "cloud":
            with open(inc_backup_log, "r", encoding="utf-8") as f:
                log_content = f.read()
                if (
                    "PXB will not be able to make a consistent backup" in log_content
                    or "PXB will not be able to take a consistent backup" in log_content
                ):
                    return  # Backup could not be completed due to DDL
                elif result and result.returncode != 0:
                    pytest.fail(
                        f"ERR: Incremental Backup failed. Please check the log at: {inc_backup_log}"
                    )

        print(
            f"..Inc backup was successfully created at: {os.path.join(self.backup_dir, 'inc')}. "
            f"Logs available at: {inc_backup_log}"
        )

        # Download incremental cloud backup if needed
        if backup_type == "cloud":
            print("=>Downloading incremental backup")
            download_inc_log = os.path.join(self.logdir, f"download_inc_backup_{log_date}_log")
            download_stream_inc_log = os.path.join(
                self.logdir, f"download_stream_inc_backup_{log_date}_log"
            )

            xbcloud_inc_cmd = [os.path.join(self.xtrabackup_dir, "xbcloud")]
            if cloud_params:
                xbcloud_inc_cmd.extend(cloud_params.split())
            xbcloud_inc_cmd.extend(["get", f"inc_backup_{log_date}"])

            with open(download_inc_log, "w", encoding="utf-8") as log_file, open(
                download_stream_inc_log, "w", encoding="utf-8"
            ) as stream_log:
                xbcloud_process = subprocess.Popen(
                    xbcloud_inc_cmd,
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                )
                xbstream_cmd = [
                    os.path.join(self.xtrabackup_dir, "xbstream"),
                    "-xv",
                    f"-C{os.path.join(self.backup_dir, 'inc')}",
                ]
                subprocess.run(
                    xbstream_cmd,
                    stdin=xbcloud_process.stdout,
                    stdout=stream_log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
                xbcloud_process.wait()
            print(f"..Incremental backup was successfully downloaded at: {os.path.join(self.backup_dir, 'inc')}")

            # Delete cloud backups
            print("=>Deleting full backup")
            delete_full_log = os.path.join(self.logdir, f"delete_full_backup_{log_date}_log")
            with open(delete_full_log, "w", encoding="utf-8") as log_file:
                subprocess.run(
                    xbcloud_inc_cmd[:-1] + ["delete", f"full_backup_{log_date}"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )

            print("=>Deleting incremental backup")
            delete_inc_log = os.path.join(self.logdir, f"delete_inc_backup_{log_date}_log")
            with open(delete_inc_log, "w", encoding="utf-8") as log_file:
                subprocess.run(
                    xbcloud_inc_cmd[:-1] + ["delete", f"inc_backup_{log_date}"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )

        # Process incremental backup
        self.process_backup(backup_type, full_backup_params, os.path.join(self.backup_dir, "inc"))

        # Save backup before prepare
        backup_save = os.path.join(HOME, "dbbackup_save")
        if os.path.exists(backup_save):
            shutil.rmtree(backup_save)
        shutil.copytree(self.backup_dir, backup_save)

        # Prepare full backup
        print("=>Preparing full backup")
        prepare_full_log = os.path.join(self.logdir, f"prepare_full_backup_{log_date}_log")
        prepare_cmd = [
            "rr",
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--no-defaults",
            "--user=root",
            "--password=",
            "--prepare",
            "--apply-log-only",
            f"--target_dir={os.path.join(self.backup_dir, 'full')}",
        ]
        if prepare_params:
            prepare_cmd.extend(prepare_params.split())

        with open(prepare_full_log, "w", encoding="utf-8") as log_file:
            subprocess.run(prepare_cmd, stderr=log_file, check=True)
        print(f"..Prepare of full backup was successful. Logs available at: {prepare_full_log}")

        # Prepare incremental backup
        print("=>Preparing incremental backup")
        prepare_inc_log = os.path.join(self.logdir, f"prepare_inc_backup_{log_date}_log")
        prepare_inc_cmd = [
            "rr",
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--no-defaults",
            "--user=root",
            "--password=",
            "--prepare",
            f"--target_dir={os.path.join(self.backup_dir, 'full')}",
            f"--incremental-dir={os.path.join(self.backup_dir, 'inc')}",
        ]
        if prepare_params:
            prepare_inc_cmd.extend(prepare_params.split())

        with open(prepare_inc_log, "w", encoding="utf-8") as log_file:
            subprocess.run(prepare_inc_cmd, stderr=log_file, check=True)
        print(f"..Prepare of incremental backup was successful. Logs available at: {prepare_inc_log}")

        # Restart server
        print("=>Restart mysql server")
        self.start_server()

        # Collect current data
        print("Collecting current data of all tables")
        rc_innodb_orig = {}
        chk_innodb_orig = {}
        rc_myrocks_orig = {}
        chk_myrocks_orig = {}

        for i in range(1, self.num_tables + 1):
            rc_result = self.mysql_execute(f"SELECT COUNT(*) FROM test.sbtest{i}", batch_mode=True)
            rc_innodb_orig[i] = int(rc_result or "0")
            chk_result = self.mysql_execute(f"CHECKSUM TABLE test.sbtest{i}", batch_mode=True)
            chk_innodb_orig[i] = int(chk_result.split()[1] if chk_result and len(chk_result.split()) > 1 else "0")

            if self.rocksdb == "enabled" and "keyring" not in mysqld_options:
                rc_result_rocks = self.mysql_execute(f"SELECT COUNT(*) FROM test_rocksdb.sbtest{i}", batch_mode=True)
                rc_myrocks_orig[i] = int(rc_result_rocks or "0")
                chk_result_rocks = self.mysql_execute(f"CHECKSUM TABLE test_rocksdb.sbtest{i}", batch_mode=True)
                chk_myrocks_orig[i] = int(chk_result_rocks.split()[1] if chk_result_rocks and len(chk_result_rocks.split()) > 1 else "0")

        # Stop server and move data directory
        print("Stopping mysql server and moving data directory")
        subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysqladmin"),
                "-uroot",
                f"-S{self.mysqldir}/socket.sock",
                "shutdown",
            ],
            check=True,
        )

        date_str = datetime.now().strftime("%d_%m_%Y")
        data_orig = os.path.join(self.mysqldir, f"data_orig_{date_str}")
        if os.path.exists(data_orig):
            shutil.rmtree(data_orig)
        shutil.move(self.datadir, data_orig)

        # Handle keyring file if needed
        if "--transition-key" in backup_params and "keyring_vault" not in mysqld_options:
            if self.keyring_file and os.path.exists(self.keyring_file):
                shutil.move(self.keyring_file, f"{self.keyring_file}_orig")

        # Handle binlog directory
        binlog_dir = os.path.join(self.mysqldir, "binlog")
        if os.path.exists(binlog_dir):
            for item in os.listdir(binlog_dir):
                shutil.move(
                    os.path.join(binlog_dir, item), os.path.join(data_orig, item)
                )
            os.rmdir(binlog_dir)

        # Restore backup
        print("=>Restoring full backup")
        restore_log = os.path.join(self.logdir, f"res_backup_{log_date}_log")
        restore_cmd = [
            os.path.join(self.xtrabackup_dir, "xtrabackup"),
            "--no-defaults",
            "--user=root",
            "--password=",
            "--copy-back",
            f"--target-dir={os.path.join(self.backup_dir, 'full')}",
            f"--datadir={self.datadir}",
        ]
        if restore_params:
            restore_cmd.extend(restore_params.split())

        with open(restore_log, "w", encoding="utf-8") as log_file:
            subprocess.run(restore_cmd, stderr=log_file, check=True)
        print(f"..Restore of full backup was successful. Logs available at: {restore_log}")

        # Copy server certificates
        for pem_file in os.listdir(data_orig):
            if pem_file.endswith(".pem"):
                shutil.copy2(os.path.join(data_orig, pem_file), self.datadir)

        # Restart server
        print("=>Restarting the mysql server")
        self.start_server()

        # Apply binlog if not encrypted
        if (
            "binlog-encryption" not in mysqld_options
            and "--encrypt-binlog" not in mysqld_options
            and "skip-log-bin" not in mysqld_options
        ):
            print("Check xtrabackup for binlog position")
            binlog_info = os.path.join(self.backup_dir, "full/xtrabackup_binlog_info")
            if os.path.exists(binlog_info):
                with open(binlog_info, "r", encoding="utf-8") as f:
                    line = f.readline().strip()
                    xb_binlog_file, xb_binlog_pos = line.split()[:2]
                    print(f"Xtrabackup binlog position: {xb_binlog_file}, {xb_binlog_pos}")

                    print(
                        f"Applying binlog to restored data starting from {xb_binlog_file}, {xb_binlog_pos}"
                    )
                    binlog_path = os.path.join(data_orig, xb_binlog_file)
                    if os.path.exists(binlog_path):
                        mysqlbinlog_cmd = [
                            os.path.join(self.mysqldir, "bin/mysqlbinlog"),
                            binlog_path,
                            f"--start-position={xb_binlog_pos}",
                        ]
                        mysql_cmd = [
                            os.path.join(self.mysqldir, "bin/mysql"),
                            "-uroot",
                            f"-S{self.mysqldir}/socket.sock",
                        ]
                        mysqlbinlog_process = subprocess.Popen(
                            mysqlbinlog_cmd, stdout=subprocess.PIPE
                        )
                        subprocess.run(mysql_cmd, stdin=mysqlbinlog_process.stdout, check=False)
                        mysqlbinlog_process.wait()
                        time.sleep(5)

        # Check restored data
        print("Checking restored data")
        print("Check the table status")
        check_err = 0
        database_list = (
            ["test", "test_rocksdb"]
            if (self.rocksdb == "enabled" and "keyring" not in mysqld_options)
            else ["test"]
        )

        for i in range(1, self.num_tables + 1):
            for database in database_list:
                table_status_result = self.mysql_execute(f"CHECK TABLE {database}.sbtest{i}")
                if not table_status_result:
                    print(f"ERR: CHECK TABLE {database}.sbtest{i} query failed")
                    # Check if database is up
                    ping_result = subprocess.run(
                        [
                            os.path.join(self.mysqldir, "bin/mysqladmin"),
                            "ping",
                            "--user=root",
                            f"--socket={self.mysqldir}/socket.sock",
                        ],
                        capture_output=True,
                    )
                    if ping_result.returncode != 0:
                        pytest.fail(
                            f"ERR: The database has gone down due to corruption in table {database}.sbtest{i}"
                        )
                    check_err = 1
                elif "OK" not in table_status_result:
                    print(
                        f"ERR: CHECK TABLE {database}.sbtest{i} query displayed the table status as '{table_status_result}'"
                    )
                    check_err = 1
                    pytest.fail("Table check failed")

        # Check if database is up
        ping_result = subprocess.run(
            [
                os.path.join(self.mysqldir, "bin/mysqladmin"),
                "ping",
                "--user=root",
                f"--socket={self.mysqldir}/socket.sock",
            ],
            capture_output=True,
        )
        if ping_result.returncode != 0:
            pytest.fail(
                "ERR: The database has gone down due to corruption, the restore was unsuccessful"
            )

        if check_err == 0:
            print("All innodb and myrocks tables status: OK")
        else:
            print("After restore, some tables may be corrupt, check table status is not OK")

        # Check record count and checksum if binlog can be applied
        if (
            "binlog-encryption" not in mysqld_options
            and "--encrypt-binlog" not in mysqld_options
            and "skip-log-bin" not in mysqld_options
        ):
            print(f"Check the record count of tables in databases: {' '.join(database_list)}")
            rc_err = 0
            checksum_err = 0

            for i in range(1, self.num_tables + 1):
                rc_innodb_res = int(
                    self.mysql_execute(f"SELECT COUNT(*) FROM test.sbtest{i}", batch_mode=True) or "0"
                )
                if rc_innodb_orig[i] != rc_innodb_res:
                    print(
                        f"ERR: The record count of test.sbtest{i} changed after restore. "
                        f"Record count in original data: {rc_innodb_orig[i]}. "
                        f"Record count in restored data: {rc_innodb_res}."
                    )
                    rc_err = 1

                if self.rocksdb == "enabled" and "keyring" not in mysqld_options:
                    rc_myrocks_res = int(
                        self.mysql_execute(f"SELECT COUNT(*) FROM test_rocksdb.sbtest{i}", batch_mode=True) or "0"
                    )
                    if rc_myrocks_orig[i] != rc_myrocks_res:
                        print(
                            f"ERR: The record count of test_rocksdb.sbtest{i} changed after restore. "
                            f"Record count in original data: {rc_myrocks_orig[i]}. "
                            f"Record count in restored data: {rc_myrocks_res}."
                        )
                        rc_err = 1

            if rc_err == 0:
                print(
                    f"Match record count of tables in databases {' '.join(database_list)} with original data: Pass"
                )

            print(f"Check the checksum of each table in databases: {' '.join(database_list)}")
            for i in range(1, self.num_tables + 1):
                chk_result = self.mysql_execute(f"CHECKSUM TABLE test.sbtest{i}", batch_mode=True)
                chk_innodb_res = int(chk_result.split()[1] if chk_result and len(chk_result.split()) > 1 else "0")
                if chk_innodb_orig[i] != chk_innodb_res:
                    print(
                        f"ERR: The checksum of test.sbtest{i} changed after restore. "
                        f"Checksum in original data: {chk_innodb_orig[i]}. "
                        f"Checksum in restored data: {chk_innodb_res}."
                    )
                    checksum_err = 1

                if self.rocksdb == "enabled" and "keyring" not in mysqld_options:
                    chk_result_rocks = self.mysql_execute(f"CHECKSUM TABLE test_rocksdb.sbtest{i}", batch_mode=True)
                    chk_myrocks_res = int(chk_result_rocks.split()[1] if chk_result_rocks and len(chk_result_rocks.split()) > 1 else "0")
                    if chk_myrocks_orig[i] != chk_myrocks_res:
                        print(
                            f"ERR: The checksum of test_rocksdb.sbtest{i} changed after restore. "
                            f"Checksum in original data: {chk_myrocks_orig[i]}. "
                            f"Checksum in restored data: {chk_myrocks_res}."
                        )
                        checksum_err = 1

            if checksum_err == 0:
                print(
                    f"Match checksum of all tables in databases {' '.join(database_list)} with original data: Pass"
                )

        # Check for gaps in primary sequence id
        print("Check for gaps in primary sequence id of tables")
        gap_found = 0
        for database in database_list:
            for i in range(1, self.num_tables + 1):
                ids_result = self.mysql_execute(
                    f"SELECT id FROM {database}.sbtest{i} ORDER BY id ASC",
                    batch_mode=True
                )
                if ids_result:
                    ids = [int(line.strip()) for line in ids_result.split("\n") if line.strip() and line.strip().isdigit()]
                    for j, id_val in enumerate(ids, start=1):
                        if id_val != j:
                            print(
                                f"ERR: Gap found in {database}.sbtest{i}. "
                                f"Expected sequence number for ID is: {j}. "
                                f"Actual sequence number for ID is: {id_val}."
                            )
                            gap_found = 1
                            return

        if gap_found == 0:
            print("No gaps found in primary sequence id of tables: Pass")

    def cleanup(self):
        """Cleanup function."""
        print("################################## CleanUp #######################################")
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

        # Cleanup vault directory
        vault_dir = os.path.join(HOME, "vault")
        if os.path.exists(vault_dir) and HOME:
            print("Cleaning up vault directory...")
            try:
                subprocess.run(["sudo", "rm", "-rf", vault_dir], check=False)
            except Exception:
                pass


# DDL operation functions (run in background threads)
def add_drop_index(helper: BackupTestHelper):
    """Add and drop an index in a table."""
    print("Add and drop an index in the test.sbtest1 table")

    def run_index_ops():
        for _ in range(10):
            ping_result = subprocess.run(
                [
                    os.path.join(helper.mysqldir, "bin/mysqladmin"),
                    "ping",
                    "--user=root",
                    f"--socket={helper.mysqldir}/socket.sock",
                ],
                capture_output=True,
            )
            if ping_result.returncode != 0:
                break

            try:
                helper.mysql_execute("CREATE INDEX kc on test.sbtest1 (k,c);")
                helper.mysql_execute("ALTER TABLE test.sbtest1 ADD INDEX kc2 (k,c);")
                helper.mysql_execute("DROP INDEX kc2 on test.sbtest1;")
                helper.mysql_execute("DROP INDEX kc on test.sbtest1;")
                helper.mysql_execute(
                    "ALTER TABLE test.sbtest1 ADD INDEX kc (k,c), ALGORITHM=COPY, LOCK=EXCLUSIVE;"
                )
                helper.mysql_execute("DROP INDEX kc on test.sbtest1;")
            except Exception:
                pass  # Ignore errors in background thread

    thread = threading.Thread(target=run_index_ops, daemon=True)
    thread.start()
    return thread


def rename_index(helper: BackupTestHelper):
    """Rename an index in a table."""
    print("Rename an index in the test.sbtest1 table")

    def run_rename_ops():
        for _ in range(10):
            ping_result = subprocess.run(
                [
                    os.path.join(helper.mysqldir, "bin/mysqladmin"),
                    "ping",
                    "--user=root",
                    f"--socket={helper.mysqldir}/socket.sock",
                ],
                capture_output=True,
            )
            if ping_result.returncode != 0:
                break

            try:
                helper.mysql_execute(
                    "ALTER TABLE test.sbtest1 RENAME INDEX k_1 TO k_2, ALGORITHM=INPLACE, LOCK=NONE;"
                )
                helper.mysql_execute(
                    "ALTER TABLE test.sbtest1 RENAME INDEX k_2 TO k_1, ALGORITHM=INPLACE, LOCK=NONE;"
                )
            except Exception:
                pass  # Ignore errors in background thread

    thread = threading.Thread(target=run_rename_ops, daemon=True)
    thread.start()
    return thread


def change_storage_engine(helper: BackupTestHelper):
    """Change the storage engine of a table."""
    print("Change the storage engine of test.sbtest1 to MYISAM, INNODB continuously")

    def run_storage_ops():
        for _ in range(10):
            ping_result = subprocess.run(
                [
                    os.path.join(helper.mysqldir, "bin/mysqladmin"),
                    "ping",
                    "--user=root",
                    f"--socket={helper.mysqldir}/socket.sock",
                ],
                capture_output=True,
            )
            if ping_result.returncode != 0:
                break

            try:
                helper.mysql_execute("alter table test.sbtest1 ENGINE=MYISAM;")
                helper.mysql_execute("alter table test.sbtest1 ENGINE=INNODB;")
            except Exception:
                pass  # Ignore errors in background thread

    thread = threading.Thread(target=run_storage_ops, daemon=True)
    thread.start()
    return thread


# Register pytest markers
def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "various_ddl_tests: Tests from Various_ddl_tests group in original bash script"
    )
    config.addinivalue_line(
        "markers", "file_encrypt_compress_stream_tests: Tests from File_encrypt_compress_stream_tests group"
    )
    config.addinivalue_line(
        "markers", "encryption_tests: Tests from encryption test groups"
    )
    config.addinivalue_line(
        "markers", "cloud_backup_tests: Tests from Cloud_backup_tests group"
    )
    config.addinivalue_line(
        "markers", "innodb_params_redo_archive_tests: Tests from Innodb_params_redo_archive_tests group"
    )
    config.addinivalue_line(
        "markers", "ssl_tests: Tests from SSL_tests group"
    )


# Pytest fixtures
@pytest.fixture(scope="function")
def test_helper():
    """Create a test helper instance."""
    helper = BackupTestHelper()
    helper.version, helper.version_normalized = helper.get_mysql_version()
    helper.pxb_version, helper.pxb_version_normalized = helper.get_pxb_version()
    helper.find_server_type()
    yield helper
    helper.cleanup()


@pytest.fixture(scope="function", autouse=True)
def setup_logdir():
    """Setup log directory."""
    if not os.path.exists(LOGDIR):
        os.makedirs(LOGDIR)


# Test functions
@pytest.mark.various_ddl_tests
def test_inc_backup(test_helper):
    """Test: Incremental Backup and Restore"""
    print("Test: Incremental Backup and Restore")
    test_helper.initialize_db()
    test_helper.incremental_backup()


@pytest.mark.various_ddl_tests
def test_add_drop_index(test_helper):
    """Test: Backup and Restore during add and drop index"""
    print("Test: Backup and Restore during add and drop index")
    test_helper.initialize_db()

    # Start DDL operations in background
    ddl_thread = add_drop_index(test_helper)
    time.sleep(2)

    # Run incremental backup
    if test_helper.version_normalized < 80000:
        if test_helper.server_type == "MS":
            test_helper.incremental_backup(backup_params="--lock-ddl-per-table")
        else:
            test_helper.incremental_backup()
    else:
        test_helper.incremental_backup()


@pytest.mark.various_ddl_tests
def test_rename_index(test_helper):
    """Test: Backup and Restore during rename index"""
    print("Test: Backup and Restore during rename index")
    test_helper.initialize_db()

    # Start DDL operations in background
    ddl_thread = rename_index(test_helper)
    time.sleep(2)

    # Run incremental backup
    test_helper.incremental_backup()


@pytest.mark.various_ddl_tests
def test_change_storage_engine(test_helper):
    """Test: Backup and Restore during change in storage engine"""
    print("Test: Backup and Restore during change in storage engine")
    test_helper.initialize_db()

    # Start DDL operations in background
    ddl_thread = change_storage_engine(test_helper)
    time.sleep(2)

    # Run incremental backup
    test_helper.incremental_backup()


@pytest.mark.file_encrypt_compress_stream_tests
def test_streaming_backup(test_helper):
    """Test: Incremental Backup and Restore with streaming"""
    print("Test: Incremental Backup and Restore with streaming")
    test_helper.initialize_db()
    test_helper.incremental_backup(
        backup_type="stream", mysqld_options="--log-bin=binlog"
    )

    if test_helper.version_normalized < 80000:
        print("Test: Incremental Backup and Restore with streaming format as tar")
        test_helper.initialize_db()
        test_helper.incremental_backup(backup_type="tar", mysqld_options="--log-bin=binlog")


@pytest.mark.file_encrypt_compress_stream_tests
def test_compress_backup(test_helper):
    """Test: Incremental Backup and Restore with compression"""
    if test_helper.version_normalized < 80000:
        pytest.skip("Compression tests not supported in PXB 2.4 and PS/MS 5.7")

    print("Test: Lz4 compression")
    test_helper.initialize_db()
    test_helper.incremental_backup(
        backup_params="--compress=lz4", mysqld_options="--log-bin=binlog"
    )

    print("Test: Zstd compression")
    test_helper.initialize_db()
    test_helper.incremental_backup(
        backup_params="--compress=zstd", mysqld_options="--log-bin=binlog"
    )


@pytest.mark.cloud_backup_tests
def test_cloud_inc_backup(test_helper):
    """Test: Incremental Backup and Restore with cloud"""
    print("Test Suite: Cloud Tests")
    test_helper.initialize_db()

    print("Test: Incremental Backup and Restore with cloud")
    test_helper.incremental_backup(
        backup_params="--parallel=10",
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    print("Test: Incremental Backup and Restore with encryption and streaming")
    test_helper.incremental_backup(
        backup_params=(
            f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
            "--encrypt-threads=10 --encrypt-chunk-size=128K"
        ),
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    # Run encryption tests for MS/PS 5.7
    if test_helper.version_normalized < 80000:
        print(f"Test: Incremental Backup and Restore for MS/PS-{test_helper.version} with keyring_file encryption")
        server_options = (
            "--early-plugin-load=keyring_file.so "
            f"--keyring_file_data={test_helper.mysqldir}/keyring "
            "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency "
            "--binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32"
        )

        test_helper.initialize_db(server_options)

        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring"
        else:
            pxb_encrypt_options = (
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
            )

        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="cloud",
            cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
        )
        print("###################################################################################")

        print(
            f"Test: Incremental Backup and Restore for MS/PS-{test_helper.version} with "
            "keyring_file encryption, quicklz compression, file encryption and streaming"
        )
        test_helper.incremental_backup(
            backup_params=(
                f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
                "--encrypt-threads=10 --encrypt-chunk-size=128K --compress --compress-threads=10"
            ),
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="cloud",
            cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
        )
        return

    print("Test: Incremental Backup and Restore with lz4 compression, encryption and streaming")
    test_helper.incremental_backup(
        backup_params=(
            f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
            "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=lz4 --compress-threads=10"
        ),
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    print("Test: Incremental Backup and Restore with zstd compression, encryption and streaming")
    test_helper.incremental_backup(
        backup_params=(
            f"--encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
            "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=zstd --compress-threads=10"
        ),
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    print(f"Test: Incremental Backup and Restore for MS/PS-{test_helper.version} with keyring_file encryption")
    rocksdb_status = test_helper.rocksdb
    test_helper.rocksdb = "disabled"  # Rocksdb tables cannot be created when encryption is enabled
    server_options = (
        "--early-plugin-load=keyring_file.so "
        f"--keyring_file_data={test_helper.mysqldir}/keyring "
        "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
        "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
        "--master_verify_checksum=ON --binlog_checksum=CRC32 "
        "--binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON"
    )

    test_helper.initialize_db(server_options)

    if test_helper.install_type == "package":
        pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring"
    else:
        pxb_encrypt_options = (
            f"--keyring_file_data={test_helper.mysqldir}/keyring "
            f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
        )

    test_helper.incremental_backup(
        backup_params=pxb_encrypt_options,
        prepare_params=pxb_encrypt_options,
        restore_params=pxb_encrypt_options,
        mysqld_options=server_options,
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    print(
        f"Test: Incremental Backup and Restore for MS/PS-{test_helper.version} with "
        "keyring_file encryption, lz4 compression, file encryption and streaming"
    )
    test_helper.incremental_backup(
        backup_params=(
            f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
            "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=lz4 --compress-threads=10"
        ),
        prepare_params=pxb_encrypt_options,
        restore_params=pxb_encrypt_options,
        mysqld_options=server_options,
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )
    print("###################################################################################")

    print(
        f"Test: Incremental Backup and Restore for MS/PS {test_helper.version} with "
        "keyring_file encryption, zstd compression, file encryption and streaming"
    )
    test_helper.incremental_backup(
        backup_params=(
            f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
            "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=zstd --compress-threads=10"
        ),
        prepare_params=pxb_encrypt_options,
        restore_params=pxb_encrypt_options,
        mysqld_options=server_options,
        backup_type="cloud",
        cloud_params=f"--defaults-file={test_helper.cloud_config} --verbose",
    )

    test_helper.rocksdb = rocksdb_status


@pytest.mark.ssl_tests
def test_ssl_backup(test_helper):
    """Test: Incremental Backup and Restore with ssl options"""
    print("Test: Incremental Backup and Restore with ssl options")

    test_helper.initialize_db()

    print("Test: Backup with SSL certificates and keys")

    # Restart server with ssl options
    add_options = (
        f"--ssl-ca={test_helper.mysqldir}/data/ca.pem "
        f"--ssl-cert={test_helper.mysqldir}/data/server-cert.pem "
        f"--ssl-key={test_helper.mysqldir}/data/server-key.pem"
    )
    test_helper.start_server(add_options)

    # Add user with ssl
    test_helper.mysql_execute("CREATE USER 'backup'@'localhost' REQUIRE SSL;")
    test_helper.mysql_execute("GRANT ALL ON *.* TO 'backup'@'localhost';")

    original_backup_user = test_helper.backup_user
    test_helper.backup_user = "backup"

    ssl_params = (
        f"--ssl-ca={test_helper.mysqldir}/data/ca.pem "
        f"--ssl-cert={test_helper.mysqldir}/data/server-cert.pem "
        f"--ssl-key={test_helper.mysqldir}/data/server-key.pem"
    )
    test_helper.incremental_backup(
        backup_params=ssl_params,
        mysqld_options=ssl_params,
    )
    print("###################################################################################")

    print("Test: Backup with SSL option --ssl-mode")
    mysql_port = test_helper.mysql_execute("select @@port;")
    if mysql_port:
        mysql_port = mysql_port.strip()

    ssl_params_with_mode = (
        f"{ssl_params} --ssl-mode=REQUIRED --host=127.0.0.1 -P {mysql_port}"
    )
    test_helper.incremental_backup(
        backup_params=ssl_params_with_mode,
        mysqld_options=ssl_params,
    )
    print("###################################################################################")

    print("Test: Backup with SSL option --ssl-cipher and --ssl-fips-mode")
    # Note: PS should be compiled with OpenSSL lib to use with --ssl-fips-mode
    # Restart server with ssl-cipher and ssl-fips-mode options
    add_options = (
        f"{ssl_params} --ssl-cipher=DHE-RSA-AES128-GCM-SHA256:AES128-SHA --ssl-fips-mode=ON"
    )
    test_helper.start_server(add_options)

    ssl_params_with_cipher = (
        f"{ssl_params} --ssl-cipher=AES128-SHA --ssl-fips-mode=ON --host=127.0.0.1 -P {mysql_port}"
    )
    server_options_with_cipher = (
        f"{ssl_params} --ssl-cipher=DHE-RSA-AES128-GCM-SHA256:AES128-SHA --ssl-fips-mode=ON"
    )
    test_helper.incremental_backup(
        backup_params=ssl_params_with_cipher,
        mysqld_options=server_options_with_cipher,
    )

    test_helper.backup_user = original_backup_user


@pytest.mark.encryption_tests
def test_inc_backup_encryption_8_0(test_helper, encrypt_type: str):
    """Test: Incremental Backup and Restore for PS 8.0+ with encryption"""
    rocksdb_status = test_helper.rocksdb
    test_helper.rocksdb = "disabled"  # Rocksdb tables cannot be created when encryption is enabled

    if encrypt_type == "keyring_file_plugin":
        if test_helper.server_type == "MS":
            server_options = (
                "--early-plugin-load=keyring_file.so "
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
                "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
                "--master_verify_checksum=ON --binlog_checksum=CRC32 "
                "--binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON"
            )
        else:
            server_options = (
                "--early-plugin-load=keyring_file.so "
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
                "--innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON "
                "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
                "--master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files "
                "--binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON"
            )

        print(
            "#################################################################################################################"
        )
        print(
            f"# Test Suite1: Incremental Backup and Restore for {test_helper.server_type}-{test_helper.version} "
            f"using PXB-{test_helper.pxb_version} with {encrypt_type} encryption #"
        )
        print(
            "#################################################################################################################"
        )

        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring"
        else:
            pxb_encrypt_options = (
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
            )

        print(f"Test1.1: Incremental Backup and Restore with basic {encrypt_type} encryption options")
        basic_server_options = (
            "--early-plugin-load=keyring_file.so "
            f"--keyring_file_data={test_helper.mysqldir}/keyring --default-table-encryption=ON"
        )
        test_helper.initialize_db(basic_server_options)
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=basic_server_options,
        )
        print("=====================================================================================")

        print(
            f"Test1.2: Incremental Backup and Restore for {test_helper.server_type}-{test_helper.version} "
            "running with all encryption options enabled"
        )
        test_helper.initialize_db(f"{server_options} --binlog-encryption")
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=f"{server_options} --binlog-encryption",
        )
        print("=====================================================================================")

        print(
            f"Test1.3: Incremental Backup and Restore for {test_helper.server_type}-{test_helper.version} "
            "using transition-key and generate-new-master-key"
        )
        lock_ddl_orig = test_helper.lock_ddl
        test_helper.lock_ddl = "on"

        if test_helper.install_type == "package":
            test_helper.incremental_backup(
                backup_params=f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key}",
                prepare_params=f"--transition-key={test_helper.encrypt_key}",
                restore_params=(
                    f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key} "
                    "--generate-new-master-key --early-plugin-load=keyring_file.so"
                ),
                mysqld_options=f"{server_options} --binlog-encryption",
            )
        else:
            test_helper.incremental_backup(
                backup_params=f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key}",
                prepare_params=(
                    f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin "
                    f"--transition-key={test_helper.encrypt_key}"
                ),
                restore_params=(
                    f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key} "
                    "--generate-new-master-key --early-plugin-load=keyring_file.so"
                ),
                mysqld_options=f"{server_options} --binlog-encryption",
            )

        test_helper.lock_ddl = lock_ddl_orig
        print("=====================================================================================")

        print(
            f"Test1.4: Incremental Backup and Restore for {test_helper.server_type}-{test_helper.version} "
            "using generate-transition-key and generate-new-master-key"
        )
        lock_ddl_orig = test_helper.lock_ddl
        test_helper.lock_ddl = "on"

        test_helper.incremental_backup(
            backup_params=f"{pxb_encrypt_options} --generate-transition-key",
            prepare_params=pxb_encrypt_options,
            restore_params=(
                f"{pxb_encrypt_options} --generate-new-master-key --early-plugin-load=keyring_file.so"
            ),
            mysqld_options=f"{server_options} --binlog-encryption",
        )
        test_helper.lock_ddl = lock_ddl_orig
        print("=====================================================================================")

        print("Test1.5: Incremental Backup and Restore with lz4 compression, encryption and streaming")
        test_helper.incremental_backup(
            backup_params=(
                f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
                "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=lz4 --compress-threads=10"
            ),
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="stream",
        )
        print("=====================================================================================")

        print("Test1.6: Incremental Backup and Restore with zstd compression, encryption and streaming")
        test_helper.incremental_backup(
            backup_params=(
                f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
                "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=zstd --compress-threads=10"
            ),
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="stream",
        )

    elif encrypt_type == "keyring_vault_plugin":
        if test_helper.server_type == "MS":
            pytest.skip(f"MS 8.0 does not support {encrypt_type} for encryption")
        elif test_helper.version_normalized >= 80100:
            pytest.skip(f"Test Suite2: {encrypt_type} is not supported in PS-{test_helper.version}")
        else:
            test_helper.start_vault_server()

        server_options = (
            "--early-plugin-load=keyring_vault=keyring_vault.so "
            f"--keyring_vault_config={test_helper.vault_config} "
            "--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON "
            "--innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON "
            "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
            "--master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files "
            "--binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON"
        )

        print(
            "################################################################################################################"
        )
        print(
            f"# Test Suite2: Incremental Backup and Restore for PS-{test_helper.version} "
            f"using PXB-{test_helper.pxb_version} with {encrypt_type} encryption #"
        )
        print(
            "################################################################################################################"
        )

        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_vault_config={test_helper.vault_config}"
        else:
            pxb_encrypt_options = (
                f"--keyring_vault_config={test_helper.vault_config} "
                f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
            )

        print(f"Test2.1: Incremental Backup and Restore with basic {encrypt_type} encryption options")
        basic_server_options = (
            "--early-plugin-load=keyring_vault=keyring_vault.so "
            f"--keyring_vault_config={test_helper.vault_config} --default-table-encryption=ON"
        )
        test_helper.initialize_db(basic_server_options)
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=basic_server_options,
        )
        print("=====================================================================================")

        print(
            f"Test2.2: Incremental Backup and Restore for PS-{test_helper.version} "
            "running with all encryption options enabled"
        )
        test_helper.initialize_db(f"{server_options} --binlog-encryption")
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=f"{server_options} --binlog-encryption",
        )
        print("=====================================================================================")

        print(
            f"Test2.3: Incremental Backup and Restore for PS-{test_helper.version} "
            "using transition-key and generate-new-master-key"
        )
        lock_ddl_orig = test_helper.lock_ddl
        test_helper.lock_ddl = "on"

        if test_helper.install_type == "package":
            test_helper.incremental_backup(
                backup_params=f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key}",
                prepare_params=f"--transition-key={test_helper.encrypt_key}",
                restore_params=(
                    f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key} "
                    "--generate-new-master-key --early-plugin-load=keyring_vault.so"
                ),
                mysqld_options=f"{server_options} --binlog-encryption",
            )
        else:
            test_helper.incremental_backup(
                backup_params=f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key}",
                prepare_params=(
                    f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin "
                    f"--transition-key={test_helper.encrypt_key}"
                ),
                restore_params=(
                    f"{pxb_encrypt_options} --transition-key={test_helper.encrypt_key} "
                    "--generate-new-master-key --early-plugin-load=keyring_vault.so"
                ),
                mysqld_options=f"{server_options} --binlog-encryption",
            )
        test_helper.lock_ddl = lock_ddl_orig
        print("=====================================================================================")

        print(
            f"Test2.4: Incremental Backup and Restore for PS-{test_helper.version} "
            "using generate-transition-key and generate-new-master-key"
        )
        lock_ddl_orig = test_helper.lock_ddl
        test_helper.lock_ddl = "on"
        test_helper.incremental_backup(
            backup_params=f"{pxb_encrypt_options} --generate-transition-key",
            prepare_params=pxb_encrypt_options,
            restore_params=(
                f"{pxb_encrypt_options} --generate-new-master-key --early-plugin-load=keyring_vault.so"
            ),
            mysqld_options=f"{server_options} --binlog-encryption",
        )
        test_helper.lock_ddl = lock_ddl_orig
        print("=====================================================================================")

        print("Test2.5: Incremental Backup and Restore with lz4 compression, encryption and streaming")
        test_helper.incremental_backup(
            backup_params=(
                f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
                "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=lz4 --compress-threads=10"
            ),
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="stream",
        )
        print("=====================================================================================")

        print("Test2.6: Incremental Backup and Restore with zstd compression, encryption and streaming")
        test_helper.incremental_backup(
            backup_params=(
                f"{pxb_encrypt_options} --encrypt=AES256 --encrypt-key={test_helper.encrypt_key} "
                "--encrypt-threads=10 --encrypt-chunk-size=128K --compress=zstd --compress-threads=10"
            ),
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
            backup_type="stream",
        )

    # Note: Additional encryption types (keyring_vault_component, keyring_file_component,
    # keyring_kmip_component, keyring_kms_component) can be added following the same pattern

    test_helper.rocksdb = rocksdb_status


@pytest.mark.encryption_tests
def test_inc_backup_encryption_2_4(test_helper, encrypt_type: str, server_type_param: str):
    """Test: Incremental Backup and Restore for PS5.7/MS5.7 with encryption"""
    rocksdb_status = test_helper.rocksdb
    test_helper.rocksdb = "disabled"  # Rocksdb tables cannot be created when encryption is enabled

    if encrypt_type == "keyring_file_plugin":
        if server_type_param == "MS":
            server_options = (
                "--early-plugin-load=keyring_file.so "
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                "--log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row "
                "--master_verify_checksum=ON --binlog_checksum=CRC32"
            )
        else:
            server_options = (
                "--early-plugin-load=keyring_file.so "
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                "--innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt "
                "--innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON "
                "--enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
                "--binlog_checksum=CRC32 --encrypt-binlog"
            )

        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_file_data={test_helper.mysqldir}/keyring"
        else:
            pxb_encrypt_options = (
                f"--keyring_file_data={test_helper.mysqldir}/keyring "
                f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
            )

        print(
            f"Test Suite1: Incremental Backup and Restore for {server_type_param}5.7 "
            f"using PXB-{test_helper.pxb_version} with keyring_file encryption"
        )

        # PXB 2.4 does not support redo log and undo log encryption
        print(f"Test: Incremental Backup and Restore when all encryption options are enabled in {server_type_param}5.7")
        test_helper.initialize_db(server_options)
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
        )

    elif encrypt_type == "keyring_vault_plugin":
        if server_type_param == "MS":
            pytest.skip(f"MS 5.7 does not support {encrypt_type} for encryption")
        else:
            test_helper.start_vault_server()

        server_options = (
            "--early-plugin-load=keyring_vault=keyring_vault.so "
            f"--keyring_vault_config={test_helper.vault_config} "
            "--innodb-encrypt-tables=ON --encrypt-tmp-files --innodb-temp-tablespace-encrypt "
            "--innodb-encrypt-online-alter-logs=ON --log-slave-updates --gtid-mode=ON "
            "--enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON "
            "--binlog_checksum=CRC32 --encrypt-binlog"
        )

        if test_helper.install_type == "package":
            pxb_encrypt_options = f"--keyring_vault_config={test_helper.vault_config}"
        else:
            pxb_encrypt_options = (
                f"--keyring_vault_config={test_helper.vault_config} "
                f"--xtrabackup-plugin-dir={test_helper.xtrabackup_dir}/../lib/plugin"
            )

        print(
            f"Test Suite2: Incremental Backup and Restore for {server_type_param}5.7 "
            f"using PXB-{test_helper.pxb_version} with keyring_vault encryption"
        )

        print(f"Test: Incremental Backup and Restore when all encryption options are enabled in {server_type_param}5.7")
        test_helper.initialize_db(server_options)
        test_helper.incremental_backup(
            backup_params=pxb_encrypt_options,
            prepare_params=pxb_encrypt_options,
            restore_params=pxb_encrypt_options,
            mysqld_options=server_options,
        )

    test_helper.rocksdb = rocksdb_status


if __name__ == "__main__":
    # Allow running as script for basic functionality
    # To run Various_ddl_tests group: pytest innodb_myrocks_backup_tests.py -m various_ddl_tests -v
    import argparse

    parser = argparse.ArgumentParser(description="PXB InnoDB MyRocks Backup Tests")
    parser.add_argument(
        "test_suites",
        nargs="*",
        choices=[
            "Various_ddl_tests",
            "File_encrypt_compress_stream_tests",
            "Encryption_PXB2_4_PS5_7_tests",
            "Encryption_PXB2_4_MS5_7_tests",
            "Encryption_PXB8_0_PS8_0_tests",
            "Encryption_PXB8_0_PS8_0_KMIP_tests",
            "Encryption_PXB8_0_PS8_0_KMS_tests",
            "Encryption_PXB8_0_MS8_0_tests",
            "Encryption_PXB9_0_PS9_0_tests",
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
        print("1. Set paths in this script for")
        print("   xtrabackup_dir, backup_dir, mysqldir, datadir, qascripts, logdir, vault_config, cloud_config")
        print("2. Set config variables in the script for")
        print("   sysbench, stream, encryption key, kmip, kms")
        print("3. Run the script as: pytest innodb_myrocks_backup_tests.py -m <marker> -v")
        print("   Or: python innodb_myrocks_backup_tests.py <Test Suites>")
        print("   Test Suites: ")
        print("   Various_ddl_tests")
        print("   File_encrypt_compress_stream_tests")
        print("   Encryption_PXB2_4_PS5_7_tests")
        print("   Encryption_PXB2_4_MS5_7_tests")
        print("   Encryption_PXB8_0_PS8_0_tests")
        print("   Encryption_PXB8_0_PS8_0_KMIP_tests")
        print("   Encryption_PXB8_0_PS8_0_KMS_tests")
        print("   Encryption_PXB8_0_MS8_0_tests")
        print("   Encryption_PXB9_0_PS9_0_tests")
        print("   Encryption_PXB9_0_MS9_0_tests")
        print("   Cloud_backup_tests")
        print("   Innodb_params_redo_archive_tests")
        print("   SSL_tests")
        print(" ")
        print("4. Logs are available at:", LOGDIR)
        sys.exit(1)

    # Run pytest with selected tests
    pytest_args = [__file__, "-v"]
    if args.verbose:
        pytest_args.append("-s")

    # Map test suites to pytest markers
    marker_mapping = {
        "Various_ddl_tests": "various_ddl_tests",
        "File_encrypt_compress_stream_tests": "file_encrypt_compress_stream_tests",
        "Cloud_backup_tests": "cloud_backup_tests",
        "SSL_tests": "ssl_tests",
        "Encryption_PXB2_4_PS5_7_tests": "encryption_tests",
        "Encryption_PXB2_4_MS5_7_tests": "encryption_tests",
        "Encryption_PXB8_0_PS8_0_tests": "encryption_tests",
        "Encryption_PXB8_0_MS8_0_tests": "encryption_tests",
        "Encryption_PXB9_0_PS9_0_tests": "encryption_tests",
        "Encryption_PXB9_0_MS9_0_tests": "encryption_tests",
    }

    for suite in args.test_suites:
        if suite in marker_mapping:
            pytest_args.extend(["-m", marker_mapping[suite]])

    pytest.main(pytest_args)

