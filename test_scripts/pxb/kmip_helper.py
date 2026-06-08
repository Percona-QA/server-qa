#!/usr/bin/env python3
"""
KMIP Helper Library
Usage: from kmip_helper import KMIPHelper

This library provides functions for managing KMIP servers (PyKMIP, HashiCorp, etc.)
Required: Docker must be installed and running
"""

import json
import os
import platform
import re
import sys
import subprocess
import time
import socket
import shutil
import uuid
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
        "pykmip": "addr=127.0.0.1,image=satyapercona/kmip:latest,port=5696,name=kmip_pykmip",
        # "fortanix": "addr=216.180.120.88,port=5696,name=kmip_fortanix,setup_script=fortanix_kmip_setup.py",
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

    @staticmethod
    def _sanitize_app_name(name: str) -> str:
        """Make a Fortanix-friendly identifier: [A-Za-z0-9_-], <= 64 chars."""
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_") or "TestingMySQL"
        return cleaned[:64]

    def _resolve_fortanix_app_name(self) -> str:
        """Pick a Fortanix app name that's safe for parallel distro runs but stable across builds.

        Resolution order:
          1. FORTANIX_APP_NAME env var (explicit override).
          2. CI fingerprint: JOB_NAME + distro + arch (stable across builds, unique per distro/arch).
             BUILD_NUMBER is intentionally NOT included so the same app is reused across builds and
             setup_app()'s delete-then-create path keeps the account at ~1 app per distro/arch
             instead of accumulating one new app per build (which can trip Fortanix subscription
             quotas, e.g. HTTP 402 from POST /sys/v1/apps).
          3. Local fallback: hostname + user + short uuid.
        """
        explicit = os.environ.get("FORTANIX_APP_NAME", "").strip()
        if explicit:
            return self._sanitize_app_name(explicit)

        job = os.environ.get("JOB_NAME", "").replace("/", "_")
        distro = ""
        if hasattr(platform, "freedesktop_os_release"):
            try:
                distro = platform.freedesktop_os_release().get("ID", "")
            except OSError:
                pass
        distro = distro or platform.system().lower()
        arch = platform.machine() or "x"

        if job:
            candidate = f"TestingMySQL-{job}-{distro}-{arch}"
        else:
            host = (platform.node().split(".")[0] or "host")
            user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
            candidate = f"TestingMySQL-{host}-{user}-{uuid.uuid4().hex[:8]}"

        return self._sanitize_app_name(candidate)

    def setup_fortanix(self) -> bool:
        """Setup Fortanix KMIP (remote server). Requires FORTANIX_EMAIL and FORTANIX_PASSWORD."""
        kmip_type = "fortanix"
        addr = self.kmip_config["addr"]
        port = self.kmip_config["port"]
        setup_script = self.kmip_config.get("setup_script", "fortanix_kmip_setup.py")
        cert_dir = self._cert_dir_path()

        email = os.environ.get("FORTANIX_EMAIL", "").strip()
        password = os.environ.get("FORTANIX_PASSWORD", "").strip()
        if not email or not password:
            self.last_error = (
                "FORTANIX_EMAIL and FORTANIX_PASSWORD environment variables must be set for Fortanix KMIP. "
                "export FORTANIX_EMAIL=your-email@example.com FORTANIX_PASSWORD=your-password"
            )
            print("ERROR: Both FORTANIX_EMAIL and FORTANIX_PASSWORD must be set for Fortanix KMIP.", file=sys.stderr)
            return False

        if os.path.exists(cert_dir):
            print(f"Cleaning existing certificate directory: {cert_dir}")
            shutil.rmtree(cert_dir, ignore_errors=True)
        os.makedirs(cert_dir, mode=0o700, exist_ok=True)

        # Per-run app name so parallel CI jobs (different OS/build) don't delete each
        # other's Fortanix app. Group name stays at the script default ("TestingMySQL").
        app_name = self._resolve_fortanix_app_name()
        self.kmip_config["app_name"] = app_name
        self.kmip_config["dsm_url"] = os.environ.get("FORTANIX_DSM_URL", "https://eu.smartkey.io").rstrip("/")
        print(f"Using Fortanix app name: {app_name}")

        # Prefer local script (same directory as this module), else download from GitHub
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, setup_script)

        if not os.path.isfile(script_path):
            print(f"Local script not found: {script_path}, downloading from GitHub...")
            try:
                import urllib.request
                url = f"https://raw.githubusercontent.com/Percona-QA/percona-qa/refs/heads/master/{setup_script}"
                with urllib.request.urlopen(url) as response:
                    script_content = response.read().decode("utf-8")
                if not script_content:
                    self.last_error = "Downloaded Fortanix setup script is empty"
                    return False
                result = subprocess.run(
                    [
                        sys.executable, "-",
                        f"--cert-dir={cert_dir}",
                        f"--email={email}",
                        f"--password={password}",
                        f"--app-name={app_name}",
                    ],
                    input=script_content,
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=script_dir,
                )
            except Exception as e:
                self.last_error = f"Fortanix setup failed: {e}"
                print(f"Failed to run Fortanix setup: {e}")
                return False
        else:
            result = subprocess.run(
                [
                    sys.executable, script_path,
                    f"--cert-dir={cert_dir}",
                    f"--email={email}",
                    f"--password={password}",
                    f"--app-name={app_name}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode != 0:
            self.last_error = f"Fortanix setup script exited with code {result.returncode}: {(result.stderr or result.stdout or '').strip()}"
            print(f"Failed to run Fortanix setup script (exit code {result.returncode})")
            if result.stderr:
                print(result.stderr)
            return False

        try:
            self.generate_kmip_config(kmip_type, addr, port, cert_dir)
        except Exception as e:
            self.last_error = "Generate KMIP config failed: " + str(e)
            print(f"Failed to generate KMIP config: {e}")
            return False

        self.kmip_config["cert_dir"] = cert_dir

        # Fortanix DSM's REST API and KMIP front-end are eventually consistent:
        # cert auth set via PATCH may not be honored on the KMIP gateway for a few
        # seconds, especially under parallel CI load. Probe with the new client cert
        # until a TLS handshake succeeds (or we hit the timeout) so mysqld doesn't
        # try to bootstrap against a still-unaware KMIP endpoint.
        timeout = int(os.environ.get("FORTANIX_KMIP_READY_TIMEOUT", "60"))
        if not self._wait_for_fortanix_kmip_ready(addr, port, cert_dir, timeout=timeout):
            print(f"Warning: Fortanix KMIP readiness could not be confirmed within {timeout}s; "
                  f"proceeding anyway (mysqld may fail to initialize the keyring).")

        print(f"Fortanix server setup successfully on address {addr} and port {port}")
        return True

    @staticmethod
    def _wait_for_fortanix_kmip_ready(addr: str, port: str, cert_dir: str, timeout: int = 60) -> bool:
        """Poll the Fortanix KMIP endpoint with the freshly-issued client cert until
        the TLS mutual-auth handshake succeeds.

        Returns True if a handshake completed, False on timeout. Best-effort: a False
        return is logged as a warning so callers can proceed and let the real
        component surface a precise error.
        """
        import ssl

        client_cert = os.path.join(cert_dir, "client_certificate.pem")
        client_key = os.path.join(cert_dir, "client_key.pem")
        server_ca = os.path.join(cert_dir, "root_certificate.pem")
        if not (os.path.isfile(client_cert) and os.path.isfile(client_key)):
            return False

        try:
            port_int = int(port)
        except (TypeError, ValueError):
            return False

        print(f"Waiting for Fortanix KMIP at {addr}:{port_int} to accept the new client cert...")
        deadline = time.monotonic() + max(timeout, 1)
        delay = 1.0
        attempts = 0
        last_error: Optional[BaseException] = None
        started = time.monotonic()

        while time.monotonic() < deadline:
            attempts += 1
            try:
                ctx = ssl.create_default_context()
                ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
                if os.path.isfile(server_ca):
                    try:
                        ctx.load_verify_locations(cafile=server_ca)
                    except ssl.SSLError:
                        pass
                # The Fortanix KMIP IP doesn't necessarily match the DSM hostname cert,
                # and our goal here is to verify *client* cert acceptance, not server identity.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

                with socket.create_connection((addr, port_int), timeout=5) as raw:
                    with ctx.wrap_socket(raw, server_hostname=addr):
                        elapsed = int(time.monotonic() - started)
                        print(f"..Fortanix KMIP accepted the new cert (after {elapsed}s, {attempts} attempt(s))")
                        return True
            except (ssl.SSLError, socket.timeout, ConnectionError, OSError) as exc:
                last_error = exc

            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

        elapsed = int(time.monotonic() - started)
        err_repr = f"{type(last_error).__name__}: {last_error}" if last_error else "no successful handshake"
        print(f"..Fortanix KMIP readiness probe gave up after {elapsed}s, {attempts} attempt(s) ({err_repr})")
        return False

    def cleanup_fortanix(self) -> bool:
        """Best-effort: delete the Fortanix app this run created.

        Uses FORTANIX_EMAIL/FORTANIX_PASSWORD to authenticate and deletes the app
        whose name was stored on self.kmip_config["app_name"] during setup_fortanix.
        Idempotent and never raises - cleanup must not mask real test results.
        """
        app_name = self.kmip_config.get("app_name") if self.kmip_config else None
        if not app_name:
            return True

        email = os.environ.get("FORTANIX_EMAIL", "").strip()
        password = os.environ.get("FORTANIX_PASSWORD", "").strip()
        if not email or not password:
            return True

        dsm_url = (self.kmip_config.get("dsm_url")
                   or os.environ.get("FORTANIX_DSM_URL", "https://eu.smartkey.io")).rstrip("/")

        try:
            import urllib.request
            import urllib.error

            def _request(method: str, path: str, token: Optional[str] = None,
                         payload: Optional[dict] = None) -> Optional[dict]:
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                data = json.dumps(payload).encode("utf-8") if payload is not None else None
                req = urllib.request.Request(f"{dsm_url}{path}", data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else {}

            auth = _request("POST", "/sys/v1/session/auth",
                            payload={"method": "password", "email": email, "password": password})
            token = (auth or {}).get("access_token")
            if not token:
                return True

            accounts = _request("GET", "/sys/v1/accounts", token=token) or []
            if accounts:
                _request("POST", "/sys/v1/session/select_account",
                         token=token, payload={"acct_id": accounts[0]["acct_id"]})

            apps = _request("GET", "/sys/v1/apps", token=token) or []
            target = next((a for a in apps if a.get("name") == app_name), None)
            if target and target.get("app_id"):
                try:
                    _request("DELETE", f"/sys/v1/apps/{target['app_id']}", token=token)
                    print(f"Cleaned up Fortanix app: {app_name}")
                except Exception as exc:
                    print(f"  Ignoring Fortanix cleanup error for app '{app_name}': {exc}")
            return True
        except Exception as exc:
            print(f"  Ignoring Fortanix cleanup error: {exc}")
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
        elif kmip_type == "fortanix":
            return self.setup_fortanix()
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

