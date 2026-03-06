#!/usr/bin/env python3

###################################################################################################
# This script runs PXB against Percona Server and MySQL server in a docker container              #
# Converted to Python with pytest                                                                 #
###################################################################################################

import subprocess
import sys
import os
import time
import shutil
from pathlib import Path

import pytest

# MySQL data directory in user's home directory
MYSQL_DATA_DIR = os.path.join(os.path.expanduser("~"), "mysql_data")


def help():
    """Print usage information and exit."""
    print("Usage: pytest docker_backup_tests.py --repo-name=REPO_NAME --repo-type=REPO_TYPE --server=SERVER [--innovation=INNOVATION]")
    print("Accepted values of repo_name: pxb-24, pxb-80, pxb-8x-innovation, pxb-84-lts, pxb-9x-innovation")
    print("Accepted values of repo_type: release, testing, experimental")
    print("Accepted value of server: ps, ms")
    print("Accepted value of innovation: 8.1, 8.2, 8.3, 8.4, 9.1")
    print("Release repo is the percona docker image and testing repo is the perconalab docker image")
    sys.exit(1)


def run_command(cmd, check=True, capture_output=False, log_file=None):
    """Run a shell command and return the result."""
    if isinstance(cmd, str):
        cmd = cmd.split()
    
    result = subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=True
    )
    
    if log_file:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"Command: {' '.join(cmd)}\n")
            if result.stdout:
                f.write(f"STDOUT: {result.stdout}\n")
            if result.stderr:
                f.write(f"STDERR: {result.stderr}\n")
    
    return result


def get_config(repo_name, repo_type, server, innovation=""):
    """Get configuration based on repo_name, repo_type, server, and innovation."""
    config = {}
    
    if repo_name == "pxb-9x-innovation":
        if server == "ms":
            config["container_name"] = f"mysql-{innovation}"
            config["mysql_docker_image"] = f"mysql:{innovation}"
        elif server == "ps":
            config["container_name"] = f"percona-server-{innovation}"
            config["mysql_docker_image"] = f"percona/percona-server:{innovation}"
        else:
            raise ValueError("Invalid product! Must be 'ps' or 'ms'")
        
        if repo_type == "release":
            config["pxb_docker_image"] = f"percona/percona-xtrabackup:{innovation}"
        elif repo_type == "testing":
            config["pxb_docker_image"] = f"perconalab/percona-xtrabackup:{innovation}"
        else:
            raise ValueError("Invalid repo_type! Must be 'release' or 'testing'")
        
        config["pxb_backup_dir"] = f"pxb_backup_data:/backup_{innovation}"
        config["target_backup_dir"] = f"/backup_{innovation}"
        config["mount_dir"] = ["-v", f"{MYSQL_DATA_DIR}:/var/lib/mysql", "-v", "/tmp/run/mysqld:/var/run/mysqld"]
    
    elif repo_name == "pxb-8x-innovation":
        if server == "ms":
            config["container_name"] = f"mysql-{innovation}"
            config["mysql_docker_image"] = f"mysql:{innovation}"
        elif server == "ps":
            config["container_name"] = f"percona-server-{innovation}"
            config["mysql_docker_image"] = f"percona/percona-server:{innovation}"
        else:
            raise ValueError("Invalid product! Must be 'ps' or 'ms'")
        
        if repo_type == "release":
            config["pxb_docker_image"] = f"percona/percona-xtrabackup:{innovation}"
        elif repo_type == "testing":
            config["pxb_docker_image"] = f"perconalab/percona-xtrabackup:{innovation}"
        else:
            raise ValueError("Invalid repo_type! Must be 'release' or 'testing'")
        
        config["pxb_backup_dir"] = f"pxb_backup_data:/backup_{innovation}"
        config["target_backup_dir"] = f"/backup_{innovation}"
        config["mount_dir"] = ["-v", f"{MYSQL_DATA_DIR}:/var/lib/mysql", "-v", "/tmp/run/mysqld:/var/run/mysqld"]
    
    elif repo_name == "pxb-80":
        if server == "ms":
            config["container_name"] = "mysql-8.0"
            config["mysql_docker_image"] = "mysql/mysql-server:8.0"
        elif server == "ps":
            config["container_name"] = "percona-server-8.0"
            config["mysql_docker_image"] = "percona/percona-server:8.0"
        else:
            raise ValueError("Invalid product! Must be 'ps' or 'ms'")
        
        if repo_type == "release":
            config["pxb_docker_image"] = "percona/percona-xtrabackup:8.0"
        elif repo_type == "testing":
            config["pxb_docker_image"] = "perconalab/percona-xtrabackup:8.0"
        else:
            raise ValueError("Invalid repo_type! Must be 'release' or 'testing'")
        
        config["pxb_backup_dir"] = "pxb_backup_data:/backup_80"
        config["target_backup_dir"] = "/backup_80"
        config["mount_dir"] = ["-v", f"{MYSQL_DATA_DIR}:/var/lib/mysql"]
    
    elif repo_name == "pxb-24":
        if server == "ms":
            config["container_name"] = "mysql-5.7"
            config["mysql_docker_image"] = "mysql/mysql-server:5.7"
        elif server == "ps":
            config["container_name"] = "percona-server-5.7"
            config["mysql_docker_image"] = "percona/percona-server:5.7"
        else:
            raise ValueError("Invalid product! Must be 'ps' or 'ms'")
        
        if repo_type == "release":
            config["pxb_docker_image"] = "percona/percona-xtrabackup:2.4"
        elif repo_type == "testing":
            config["pxb_docker_image"] = "perconalab/percona-xtrabackup:2.4"
        else:
            raise ValueError("Invalid repo_type! Must be 'release' or 'testing'")
        
        config["pxb_backup_dir"] = "pxb_backup_data:/backup"
        config["target_backup_dir"] = "/backup"
        config["mount_dir"] = ["-v", f"{MYSQL_DATA_DIR}:/var/lib/mysql"]
    
    elif repo_name == "pxb-84-lts":
        if server == "ms":
            config["container_name"] = "mysql-8.4"
            config["mysql_docker_image"] = "mysql:8.4.2"
        elif server == "ps":
            config["container_name"] = "percona-server-8.4"
            config["mysql_docker_image"] = "percona/percona-server:8.4"
        else:
            raise ValueError("Invalid product! Must be 'ps' or 'ms'")
        
        if repo_type == "release":
            config["pxb_docker_image"] = "percona/percona-xtrabackup:8.4.0-1"
        elif repo_type == "testing":
            config["pxb_docker_image"] = "perconalab/percona-xtrabackup:8.4.0-1"
        else:
            raise ValueError("Invalid repo_type! Must be 'release' or 'testing'")
        
        config["pxb_backup_dir"] = "pxb_backup_data:/backup_84"
        config["target_backup_dir"] = "/backup_84"
        config["mount_dir"] = ["-v", f"{MYSQL_DATA_DIR}:/var/lib/mysql", "-v", "/tmp/run/mysqld:/var/run/mysqld"]
    
    else:
        raise ValueError("Invalid version parameter. Exiting")
    
    return config


def clean_setup(config, log_file=None):
    """Check and clean the setup."""
    container_name = config["container_name"]
    
    # Check if container exists and stop/remove it
    result = subprocess.run(
        ["sudo", "docker", "ps", "-a"],
        capture_output=True,
        text=True
    )
    
    if container_name in result.stdout:
        run_command(["sudo", "docker", "stop", container_name], check=False, log_file=log_file)
        run_command(["sudo", "docker", "rm", container_name], check=False, log_file=log_file)
    
    # Remove mysql_data directory if it exists
    if os.path.exists(MYSQL_DATA_DIR):
        run_command(["sudo", "rm", "-rf", MYSQL_DATA_DIR], check=False, log_file=log_file)

    if log_file:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("Removing all images and volumes not being used by any container\n")
    
    run_command(["sudo", "docker", "image", "prune", "-a", "-f"], check=False, log_file=log_file)
    run_command(["sudo", "docker", "volume", "prune", "-f"], check=False, log_file=log_file)


def wait_for_mysql_start(container_name, max_wait=180, log_file=None):
    """Wait for MySQL container to start and be ready."""
    print("Waiting for mysql to start...")
    for i in range(1, max_wait + 1):
        # Check if container exists and get its status
        result = subprocess.run(
            ["sudo", "docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Status}}"],
            capture_output=True,
            text=True
        )
        
        status = result.stdout.strip()

        # Check if container exited
        if status and "Exited" in status:
            # Container exited, check logs
            log_result = subprocess.run(
                ["sudo", "docker", "logs", "--tail", "50", container_name],
                capture_output=True,
                text=True
            )
            error_msg = f"Container {container_name} exited. Logs:\n{log_result.stdout}\n{log_result.stderr}"
            if log_file:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"ERROR: {error_msg}\n")
            raise RuntimeError(f"The mysql container exited unexpectedly. {error_msg}")

        # Check if container is running
        if status and "Up" in status:
            # Try to connect to MySQL to verify it's ready
            try:
                test_result = subprocess.run(
                    ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-e", "SELECT 1;"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False
                )
                if test_result.returncode == 0:
                    print(f"MySQL container {container_name} is ready")
                    return True
            except subprocess.TimeoutExpired:
                # MySQL not ready yet, continue waiting
                pass
            except Exception:
                # Other errors, continue waiting
                pass
        
        time.sleep(1)
        
        if i == max_wait:
            # Check logs before failing
            log_result = subprocess.run(
                ["sudo", "docker", "logs", "--tail", "50", container_name],
                capture_output=True,
                text=True
            )
            error_msg = f"Container {container_name} failed to start. Status: {status}. Logs:\n{log_result.stdout}\n{log_result.stderr}"
            if log_file:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"ERROR: {error_msg}\n")
            raise RuntimeError(f"The mysql server failed to start in docker container. {error_msg}")
    
    return True


def wait_for_mysql_container(container_name, max_wait=180):
    """Wait for MySQL container to exist."""
    print("Waiting for mysql to start...")
    for i in range(1, max_wait + 1):
        result = subprocess.run(
            ["sudo", "docker", "ps", "-a"],
            capture_output=True,
            text=True
        )
        
        if container_name in result.stdout:
            return True
        
        time.sleep(1)
        
        if i == max_wait:
            raise RuntimeError("The mysql server failed to start with the restored data in the docker container")
    
    return True


@pytest.fixture(scope="function")
def test_config(request):
    """Fixture to get test configuration from command-line options."""
    repo_name = request.config.getoption("--repo-name")
    repo_type = request.config.getoption("--repo-type")
    server = request.config.getoption("--server")
    innovation = request.config.getoption("--innovation")

    # Validate required options
    if not repo_name:
        pytest.fail("ERR: '--repo-name' argument is required. Accepted values: pxb-24, pxb-80, pxb-8x-innovation, pxb-84-lts, pxb-9x-innovation")
    if not repo_type:
        pytest.fail("ERR: '--repo-type' argument is required. Accepted values: release, testing, experimental")
    if not server:
        pytest.fail("ERR: '--server' argument is required. Accepted values: ps, ms")

    # Validate innovation requirement for pxb-8x-innovation and pxb-9x-innovation
    if repo_name in ("pxb-8x-innovation", "pxb-9x-innovation") and not innovation:
        pytest.fail(f"ERR: '--innovation' argument is required for {repo_name}. Accepted values: 8.1, 8.2, 8.3, 8.4, 9.1")

    try:
        config = get_config(repo_name, repo_type, server, innovation)
        config["repo_type"] = repo_type  # Store repo_type in config for use in test
        return config
    except ValueError as e:
        pytest.fail(str(e))


@pytest.fixture(scope="function")
def log_file():
    """Fixture to manage log file."""
    log_path = Path("backup_log")
    if log_path.exists():
        log_path.unlink()
    
    yield str(log_path)
    
    # Log file cleanup handled by test


def test_pxb_docker(test_config, log_file):
    """Test PXB docker backup and restore."""
    config = test_config
    container_name = config["container_name"]
    mysql_docker_image = config["mysql_docker_image"]
    pxb_docker_image = config["pxb_docker_image"]
    pxb_backup_dir = config["pxb_backup_dir"]
    target_backup_dir = config["target_backup_dir"]
    mount_dir = config["mount_dir"]
    
    # Build docker run command
    start_mysql_cmd = [
        "sudo", "docker", "run",
        "--name", container_name
    ] + mount_dir + [
        "-p", "3306:3306",
        "-e", "PERCONA_TELEMETRY_DISABLE=1",
        "-e", "MYSQL_ROOT_HOST=%",
        "-e", "MYSQL_ROOT_PASSWORD=mysql",
        "-d",
        mysql_docker_image
    ]
    
    print(f"Run {container_name} docker container")
    print(f"Command: {' '.join(start_mysql_cmd)}")
    
    # Remove mysql_data directory if it exists to ensure clean start
    if os.path.exists(MYSQL_DATA_DIR):
        run_command(["sudo", "rm", "-rf", MYSQL_DATA_DIR], check=False, log_file=log_file)
    
    # Create mysql_data directory fresh (MySQL will initialize it)
    os.makedirs(MYSQL_DATA_DIR, exist_ok=True)
    os.chmod(MYSQL_DATA_DIR, 0o777)
    
    # Create /tmp/run/mysqld if needed (mounted as /var/run/mysqld in container)
    if "/var/run/mysqld" in str(mount_dir):
        if os.path.exists("/tmp/run/mysqld"):
            shutil.rmtree("/tmp/run/mysqld")
        os.makedirs("/tmp/run/mysqld", exist_ok=True)
        os.chmod("/tmp/run/mysqld", 0o777)

    # Start MySQL container
    try:
        run_command(start_mysql_cmd, log_file=log_file)
    except subprocess.CalledProcessError:
        pytest.fail(f"ERR: The docker command to start {container_name} failed")

    # Wait for MySQL to start
    wait_for_mysql_start(container_name, log_file=log_file)
    
    # Sleep for server to fully come up
    time.sleep(20)

    # Get MySQL version
    result = run_command(
        ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-Bse", "SELECT @@version;"],
        capture_output=True
    )
    version = result.stdout.strip().replace("Using a password", "").strip()
    print(f"Mysql started with version: {version}")
    
    # Add data to database
    print("Add data in the database")
    run_command(
        ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-e", "CREATE DATABASE IF NOT EXISTS test;"],
        check=False,
        log_file=log_file
    )
    run_command(
        ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-e", "CREATE TABLE test.t1(i INT);"],
        check=False,
        log_file=log_file
    )
    run_command(
        ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-e", "INSERT INTO test.t1 VALUES (1), (2), (3), (4), (5);"],
        check=False,
        log_file=log_file
    )

    # Run PXB backup and prepare
    print("Run pxb docker container, take backup and prepare it")
    print(f"Using {config['repo_type']} repo docker image")

    backup_cmd = [
        "sudo", "docker", "run",
        "--volumes-from", container_name,
        "-v", pxb_backup_dir,
        "--rm", "--user", "root",
        pxb_docker_image,
        "/bin/bash", "-c",
        f"rm -rf {target_backup_dir}/* ; xtrabackup --backup --datadir=/var/lib/mysql/ --target-dir={target_backup_dir} --user=root --password=mysql ; xtrabackup --prepare --target-dir={target_backup_dir}"
    ]

    try:
        run_command(backup_cmd, log_file=log_file)
        print(f"The backup and prepare was successful. Log available at: {os.path.abspath(log_file)}")
    except subprocess.CalledProcessError:
        pytest.fail("ERR: The docker command to run PXB failed")

    # Stop the container
    print(f"Stop the {container_name} docker container")
    run_command(["sudo", "docker", "stop", container_name], log_file=log_file)

    # Remove and recreate mysql_data directory
    if os.path.exists(MYSQL_DATA_DIR):
        run_command(["sudo", "rm", "-rf", MYSQL_DATA_DIR], check=False, log_file=log_file)
    os.makedirs(MYSQL_DATA_DIR, exist_ok=True)
    
    # Restore backup
    print("Run pxb docker container to restore the backup")
    print(f"Using {config['repo_type']} repo docker image")
    
    restore_cmd = [
        "sudo", "docker", "run",
        "--volumes-from", container_name,
        "-v", pxb_backup_dir,
        "--rm", "--user", "root",
        pxb_docker_image,
        "/bin/bash", "-c",
        f"xtrabackup --copy-back --datadir=/var/lib/mysql/ --target-dir={target_backup_dir}"
    ]

    try:
        run_command(restore_cmd, log_file=log_file)
        print("The restore command was successful")
    except subprocess.CalledProcessError:
        pytest.fail("ERR: The docker command to restore the data failed")
    
    # Set permissions using sudo (files were created by MySQL user in container)
    run_command(["sudo", "chmod", "-R", "777", MYSQL_DATA_DIR], check=False, log_file=log_file)
    
    # Start container with restored data
    print(f"Start the {container_name} container with the restored data")
    try:
        run_command(["sudo", "docker", "start", container_name], log_file=log_file)
    except subprocess.CalledProcessError:
        pytest.fail(f"ERR: The docker command to start {container_name} with the restored data failed")
    
    # Wait for MySQL to start
    wait_for_mysql_container(container_name)
    
    # Sleep for server to fully come up
    time.sleep(20)
    
    # Verify data
    result = run_command(
        ["sudo", "docker", "exec", container_name, "mysql", "-uroot", "-pmysql", "-Bse", "SELECT * FROM test.t1;"],
        capture_output=True
    )
    
    # Count non-empty lines (excluding password warning)
    lines = [line for line in result.stdout.split('\n') if line.strip() and "password" not in line.lower()]
    row_count = len(lines)
    
    if row_count != 5:
        pytest.fail(f"ERR: Data could not be checked in the mysql container. Expected 5 rows, got {row_count}")
    else:
        print("Data was restored successfully")
    
    # Cleanup
    print(f"Stopping and removing {container_name} docker container")
    run_command(["sudo", "docker", "stop", container_name], check=False, log_file=log_file)
    run_command(["sudo", "docker", "rm", container_name], check=False, log_file=log_file)


@pytest.fixture(scope="function", autouse=True)
def setup_and_teardown(test_config, log_file):
    """Function-level setup and teardown."""
    # Setup - initial cleanup
    clean_setup(test_config, log_file)
    
    yield
    
    # Teardown - final cleanup
    clean_setup(test_config, log_file)
    print(f"Logs for the tests are available at: {os.path.abspath(log_file)}")


if __name__ == "__main__":
    # Allow running as a script for easier debugging
    import argparse
    
    parser = argparse.ArgumentParser(description="PXB Docker Backup Tests")
    parser.add_argument("--repo-name", required=True, help="Repo name")
    parser.add_argument("--repo-type", required=True, help="Repo type")
    parser.add_argument("--server", required=True, help="Server type")
    parser.add_argument("--innovation", default="", help="Innovation version")
    
    args = parser.parse_args()
    
    # Run pytest with the arguments
    pytest.main([
        __file__,
        f"--repo-name={args.repo_name}",
        f"--repo-type={args.repo_type}",
        f"--server={args.server}",
        f"--innovation={args.innovation}",
        "-v"
    ])

