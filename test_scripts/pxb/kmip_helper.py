#!/usr/bin/env python3
"""
KMIP Helper Library
Usage: from kmip_helper import KMIPHelper

This library provides functions for managing KMIP servers (PyKMIP, HashiCorp, etc.)
Required: Docker must be installed and running
"""

import os
import sys
import subprocess
import time
import socket
import shutil
from typing import Dict, List, Optional


def _fully_qualified_image(image: str) -> str:
    """Return a fully qualified image name for Podman/short-name resolution (no TTY)."""
    if not image:
        return image
    # If the first segment (registry) contains a dot, it's already qualified (e.g. docker.io, quay.io).
    first = image.split("/")[0]
    if "." in first or first == "localhost":
        return image
    return "docker.io/" + image


class KMIPHelper:
    """Helper class for managing KMIP servers."""

    # Default KMIP configurations
    DEFAULT_KMIP_CONFIGS = {
        "pykmip": "addr=127.0.0.1,image=mohitpercona/kmip:latest,port=5696,name=kmip_pykmip",
        # "hashicorp": "addr=127.0.0.1,port=5696,name=kmip_hashicorp,setup_script=hashicorp-kmip-setup.sh",
        # "ciphertrust": "addr=127.0.0.1,port=5696,name=kmip_ciphertrust,setup_script=setup_kmip_api.py",
    }

    def __init__(
        self,
        kmip_configs: Optional[Dict[str, str]] = None,
        cert_base_dir: Optional[str] = None,
    ):
        """Initialize KMIP helper with configurations.
        cert_base_dir: If set, KMIP cert dirs are created under this path (e.g. TEST_BASE_DIR); else under ~.
        """
        self.kmip_configs = kmip_configs or self.DEFAULT_KMIP_CONFIGS.copy()
        self.kmip_container_names: List[str] = []
        self.kmip_config: Dict[str, str] = {}
        self.cert_base_dir = cert_base_dir
        self.last_error: str = ""  # Set before return False to surface failure reason

    def init_kmip_configs(self):
        """Initialize default configurations if not already set."""
        if not self.kmip_configs:
            self.kmip_configs = self.DEFAULT_KMIP_CONFIGS.copy()
            print("Initialized default KMIP configurations", file=sys.stderr)

    def cleanup_existing_container(self, container_name: str) -> bool:
        """Cleanup existing Docker container."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name={container_name}"],
                capture_output=True,
                text=True,
                check=False,
            )
            container_id = result.stdout.strip()

            if not container_id:
                return True

            subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            time.sleep(5)  # Allow port to be released
            return True
        except Exception:
            return False

    def validate_port_available(self, port: str) -> bool:
        """Validate if a port is available."""
        if not port:
            print("Error: No port specified")
            return False

        max_attempts = 10
        for i in range(1, max_attempts + 1):
            port_in_use = False

            # Method 1: Fast TCP check
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(("127.0.0.1", int(port)))
                sock.close()
                if result == 0:
                    port_in_use = True
            except Exception:
                pass

            # Fallback: Use ss or netstat
            if not port_in_use:
                try:
                    if shutil.which("ss"):
                        result = subprocess.run(
                            ["ss", "-tuln"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if f":{port}" in result.stdout:
                            port_in_use = True
                    elif shutil.which("netstat"):
                        result = subprocess.run(
                            ["netstat", "-tuln"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if f":{port}" in result.stdout:
                            port_in_use = True
                except Exception:
                    pass

            if not port_in_use:
                return True

            print(".", end="", flush=True)
            if i < max_attempts:
                time.sleep(2)

        print()
        return False

    def validate_environment(self, kmip_type: str) -> bool:
        """Validate KMIP type."""
        if not kmip_type:
            self.last_error = "No KMIP type specified"
            print("ERROR: No KMIP type specified", file=sys.stderr)
            return False

        if kmip_type not in self.kmip_configs:
            self.last_error = f"Invalid type '{kmip_type}'. Available: {list(self.kmip_configs.keys())}"
            print(f"ERROR: Invalid type '{kmip_type}'. Available types:", file=sys.stderr)
            for key in self.kmip_configs.keys():
                print(f"  - {key}", file=sys.stderr)
            return False

        return True

    def get_kmip_container_names(self):
        """Get all KMIP container names."""
        self.kmip_container_names = []
        for kmip_type in self.kmip_configs.keys():
            config_str = self.kmip_configs[kmip_type]
            pairs = config_str.split(",")
            for pair in pairs:
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    if key == "name":
                        self.kmip_container_names.append(value)
                        break

    def parse_config(self, kmip_type: str):
        """Parse configuration for a specific type."""
        self.kmip_config = {}
        config_str = self.kmip_configs[kmip_type]

        pairs = config_str.split(",")
        for pair in pairs:
            if "=" in pair:
                key, value = pair.split("=", 1)
                self.kmip_config[key] = value

        # Set defaults if not specified
        self.kmip_config["type"] = kmip_type
        if "name" not in self.kmip_config:
            self.kmip_config["name"] = f"kmip_{kmip_type}"
        if "addr" not in self.kmip_config:
            self.kmip_config["addr"] = "127.0.0.1"
        if "port" not in self.kmip_config:
            self.kmip_config["port"] = "5696"
        if "cert_dir" not in self.kmip_config:
            self.kmip_config["cert_dir"] = f"kmip_certs_{kmip_type}"

    def _cert_dir_path(self) -> str:
        """Return full path to the cert directory for current kmip_config."""
        base = self.cert_base_dir or os.path.expanduser("~")
        return os.path.join(base, self.kmip_config["cert_dir"])

    def generate_kmip_config(self, kmip_type: str, addr: str, port: str, cert_dir: str):
        """Generate KMIP configuration file."""
        config_file = os.path.join(cert_dir, "component_keyring_kmip.cnf")
        print(f"Generating KMIP config for: {kmip_type}")

        config_content = f"""{{
  "server_addr": "{addr}",
  "server_port": "{port}",
  "client_ca": "{cert_dir}/client_certificate.pem",
  "client_key": "{cert_dir}/client_key.pem",
  "server_ca": "{cert_dir}/root_certificate.pem"
}}
"""
        with open(config_file, "w") as f:
            f.write(config_content)
        print(f"Configuration file created: {config_file}")

    def setup_pykmip(self) -> bool:
        """Setup PyKMIP server."""
        kmip_type = "pykmip"
        container_name = self.kmip_config["name"]
        addr = self.kmip_config["addr"]
        port = self.kmip_config["port"]
        image = self.kmip_config.get("image", "")
        cert_dir = self._cert_dir_path()

        if os.path.exists(cert_dir):
            print(f"Cleaning existing certificate directory: {cert_dir}")
            shutil.rmtree(cert_dir, ignore_errors=True)

        os.makedirs(cert_dir, mode=0o700, exist_ok=True)

        # Cleanup existing resources
        print("Cleaning up existing container... ", end="", flush=True)
        if self.cleanup_existing_container(container_name):
            print("Done")
        else:
            self.last_error = "Cleanup of existing container failed"
            print("Failed")
            return False

        # Verify port availability
        print(f"Checking port {port} availability... ", end="", flush=True)
        if self.validate_port_available(port):
            print("Available")
        else:
            print("Unavailable")
            print(f"Port {port} is in use by:")
            try:
                subprocess.run(["lsof", "-i", f":{port}"], check=False)
            except Exception:
                pass

            self.get_kmip_container_names()
            for kmip_name in self.kmip_container_names:
                self.cleanup_existing_container(kmip_name)

            if not self.validate_port_available(port):
                self.last_error = f"Port {port} still in use after cleanup. Check and free the port."
                print(f"Still unavailable {port}, please check and clean up port {port} and retry")
                return False

        # Start container (use FQIN so Podman does not prompt for short-name resolution without TTY)
        print("Starting container... ", end="", flush=True)
        image_fq = _fully_qualified_image(image)
        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container_name,
                    "--security-opt",
                    "seccomp=unconfined",
                    "--cap-add=NET_ADMIN",
                    "-p",
                    f"{port}:5696",
                    image_fq,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip() or f"docker run exited with code {result.returncode}"
                self.last_error = "Docker run failed: " + err
                print("Failed")
                print(result.stderr)
                return False

            # Get container ID
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", container_name],
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"Started (ID: {result.stdout.strip()})")
        except Exception as e:
            self.last_error = "Docker start failed: " + str(e)
            print("Failed")
            return False

        time.sleep(10)

        # Copy certificates
        try:
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:/opt/certs/root_certificate.pem",
                    os.path.join(cert_dir, "root_certificate.pem"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:/opt/certs/client_key_jane_doe.pem",
                    os.path.join(cert_dir, "client_key.pem"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:/opt/certs/client_certificate_jane_doe.pem",
                    os.path.join(cert_dir, "client_certificate.pem"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        time.sleep(5)

        # Generate KMIP configuration
        print("Generating KMIP configuration...")
        try:
            self.generate_kmip_config(kmip_type, addr, port, cert_dir)
        except Exception as e:
            self.last_error = "Generate KMIP config failed: " + str(e)
            print(f"Failed to generate KMIP config: {e}")
            return False

        self.kmip_config["cert_dir"] = cert_dir
        print(f"PyKMIP server started successfully on address {addr} and port {port}")
        return True

    def setup_hashicorp(self) -> bool:
        """Setup HashiCorp Vault KMIP server."""
        kmip_type = "hashicorp"
        container_name = self.kmip_config["name"]
        addr = self.kmip_config["addr"]
        port = self.kmip_config["port"]
        setup_script = self.kmip_config.get("setup_script", "")
        cert_dir = self._cert_dir_path()

        print("Cleaning up existing container... ", end="", flush=True)
        if self.cleanup_existing_container(container_name):
            print("Done")
        else:
            print("Failed")
            return False

        print(f"Checking port {port} availability... ", end="", flush=True)
        if self.validate_port_available(port):
            print("Available")
        else:
            print("Unavailable")
            print(f"Port {port} is in use by:")
            try:
                subprocess.run(["lsof", "-i", f":{port}"], check=False)
            except Exception:
                pass

            self.get_kmip_container_names()
            for name in self.kmip_container_names:
                self.cleanup_existing_container(name)

            if not self.validate_port_available(port):
                print(f"Still unavailable {port}, please check and clean up port {port} and retry")
                return False

        print(f"Starting Docker KMIP server in (script method): {setup_script}")
        # Download and execute the hashicorp setup
        try:
            import urllib.request

            url = f"https://raw.githubusercontent.com/Percona-QA/percona-qa/refs/heads/master/{setup_script}"
            with urllib.request.urlopen(url) as response:
                script_content = response.read().decode("utf-8")

            if not script_content:
                print("Downloaded script is empty")
                return False

            if os.path.exists(cert_dir):
                print(f"Cleaning existing certificate directory: {cert_dir}")
                shutil.rmtree(cert_dir, ignore_errors=True)

            os.makedirs(cert_dir, exist_ok=True)

            # Execute the script
            result = subprocess.run(
                ["bash", "-s", "--", f"--cert-dir={cert_dir}"],
                input=script_content,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                print(f"Failed to execute script {setup_script}, (exit code: {result.returncode})")
                return False

            self.generate_kmip_config(kmip_type, addr, port, cert_dir)
        except Exception as e:
            print(f"Failed to setup HashiCorp: {e}")
            return False

        self.kmip_config["cert_dir"] = cert_dir
        print(f"Hashicorp server started successfully on address {addr} and port {port}")
        return True

    def setup_cipher_api(self) -> bool:
        """Placeholder for CipherTrust setup."""
        print("CipherTrust setup not implemented yet")
        return False

    def start_kmip_server(self, kmip_type: str) -> bool:
        """Main function to start KMIP server."""
        self.last_error = ""
        if not self.validate_environment(kmip_type):
            return False

        self.parse_config(kmip_type)
        print(f"Starting {kmip_type.upper()} KMIP Server on port {self.kmip_config['port']}")

        if kmip_type == "pykmip":
            return self.setup_pykmip()
        elif kmip_type == "hashicorp":
            return self.setup_hashicorp()
        elif kmip_type == "ciphertrust":
            return self.setup_cipher_api()
        else:
            self.last_error = f"Unsupported KMIP type: {kmip_type}"
            print(f"Unsupported KMIP Type: {kmip_type}")
            return False


# For backward compatibility, provide module-level functions
_kmip_helper_instance = None


def get_kmip_helper(kmip_configs: Optional[Dict[str, str]] = None) -> KMIPHelper:
    """Get or create a singleton KMIP helper instance."""
    global _kmip_helper_instance
    if _kmip_helper_instance is None:
        _kmip_helper_instance = KMIPHelper(kmip_configs)
    return _kmip_helper_instance

