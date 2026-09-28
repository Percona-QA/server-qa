#!/usr/bin/env python3
"""
SeaweedFS Helper Library
Usage: from seaweedfs_helper import SeaweedFSHelper

Manages a local SeaweedFS S3-gateway container used as the S3-compatible
backend for xbstream_fifo_tests.py's cloud/FIFO backup tests. Ported from
xbstream_fifo_test.sh's start_seaweedfs()/cleanup_exit() shell functions.
Required: Docker must be installed and running.
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from typing import Optional

CONTAINER_NAME = "seaweedfs"
IMAGE = "chrislusf/seaweedfs:latest"
HOST_PORT = 9000
CONTAINER_S3_PORT = 8333
S3_ACCESS_KEY = "admin"
S3_SECRET_KEY = "password"


class SeaweedFSHelper:
    """Helper class for starting/stopping the SeaweedFS S3-gateway container."""

    def __init__(self, data_dir: Optional[str] = None, host_port: int = HOST_PORT):
        self.data_dir = data_dir or os.path.join(os.path.expanduser("~"), "seaweedfs", "data")
        self.config_dir = os.path.dirname(self.data_dir)
        self.host_port = host_port
        self.last_error: str = ""

    def _container_status(self) -> Optional[str]:
        """Return 'running', 'stopped', or None if the container doesn't exist."""
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTAINER_NAME}", "--filter", "status=running",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        )
        if CONTAINER_NAME in result.stdout.split():
            return "running"
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={CONTAINER_NAME}", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        )
        if CONTAINER_NAME in result.stdout.split():
            return "stopped"
        return None

    def _write_s3_config(self) -> str:
        """Write the S3 identity config (admin/password) SeaweedFS's S3 gateway needs."""
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
        config = {
            "identities": [
                {
                    "name": "admin",
                    "credentials": [{"accessKey": S3_ACCESS_KEY, "secretKey": S3_SECRET_KEY}],
                    "actions": ["Admin", "Read", "Write"],
                }
            ]
        }
        config_path = os.path.join(self.config_dir, "s3.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return config_path

    def _wait_ready(self, timeout: int = 60) -> bool:
        """Poll the S3 gateway port. SeaweedFS has no MinIO-style
        /minio/health/ready endpoint, so any HTTP response (even an error
        status) is enough to know xbcloud can reach it."""
        url = f"http://localhost:{self.host_port}/"
        for _ in range(timeout):
            try:
                urllib.request.urlopen(url, timeout=1)
                return True
            except urllib.error.HTTPError:
                # Any HTTP response (e.g. 403/404) means the gateway is up.
                return True
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        return False

    def start(self) -> bool:
        """Start the SeaweedFS container if it isn't already running."""
        status = self._container_status()
        if status == "running":
            print("SeaweedFS is already running.")
        elif status == "stopped":
            print("Found stopped SeaweedFS container. Starting it...")
            result = subprocess.run(
                ["docker", "start", CONTAINER_NAME],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                self.last_error = result.stderr or result.stdout
                print(f"ERR: Failed to start SeaweedFS container: {self.last_error}")
                return False
        else:
            if os.path.isdir(self.data_dir):
                for entry in os.listdir(self.data_dir):
                    path = os.path.join(self.data_dir, entry)
                    subprocess.run(["rm", "-rf", path], check=False)
            config_path = self._write_s3_config()
            print("No SeaweedFS container found. Creating and starting one...")
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "-p", f"{self.host_port}:{CONTAINER_S3_PORT}",
                    "--name", CONTAINER_NAME,
                    "-v", f"{self.data_dir}:/data",
                    "-v", f"{config_path}:/etc/seaweedfs/s3.json",
                    IMAGE,
                    "server", "-s3", f"-s3.port={CONTAINER_S3_PORT}",
                    "-s3.config=/etc/seaweedfs/s3.json", "-dir=/data",
                ],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                self.last_error = result.stderr
                print(f"ERR: Failed to start SeaweedFS container: {result.stderr}")
                return False

        print("Waiting for SeaweedFS to become ready", end="", flush=True)
        if self._wait_ready():
            print("\nSeaweedFS is ready!\n")
            return True

        print("\nSeaweedFS failed to become ready in time.")
        logs = subprocess.run(["docker", "logs", CONTAINER_NAME], capture_output=True, text=True, check=False)
        self.last_error = logs.stdout + logs.stderr
        print(self.last_error)
        return False

    def stop(self) -> None:
        """Stop the SeaweedFS container if it's running."""
        if self._container_status() == "running":
            print("Stopping SeaweedFS container...")
            subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True, check=False)
