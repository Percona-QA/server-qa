import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerHelper:
    def __init__(self, cli: str | None = None):
        if cli is None:
            cli = os.environ.get("CONTAINER_CLI")
        if cli is None:
            for candidate in ("docker", "podman"):
                if shutil.which(candidate):
                    cli = candidate
                    break
        if not cli:
            raise RuntimeError(
                "No container CLI found. Install docker or podman, or set CONTAINER_CLI."
            )
        self.cli = cli

    def _run(self, args: list[str], check: bool = True, input_text: str | None = None) -> ExecResult:
        """Run a container CLI command and return its result, raising on failure when check is set."""
        proc = subprocess.run(
            [self.cli, *args],
            capture_output=True,
            text=True,
            input=input_text,
        )
        result = ExecResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)
        if check and not result.ok:
            raise RuntimeError(
                f"{self.cli} {' '.join(args)} failed (exit {proc.returncode})\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return result

    def create(
        self,
        image: str,
        name: str,
        hostname: str | None = None,
        environment: dict[str, str] | None = None,
        volumes: list[str] | None = None,
        networks: list[str] | None = None,
        ports: list[str] | None = None,
        command: list[str] | None = None,
        detach: bool = True,
        restart: str | None = None,
    ) -> ExecResult:
        """Create and start a long-lived (detached) container with the given config."""
        args = ["run"]
        if detach:
            args.append("-d")
        args.extend(["--name", name])
        if hostname:
            args.extend(["--hostname", hostname])
        if restart:
            args.extend(["--restart", restart])
        for k, v in (environment or {}).items():
            args.extend(["-e", f"{k}={v}"])
        for vol in volumes or []:
            args.extend(["-v", vol])
        for net in networks or []:
            args.extend(["--network", net])
        for port in ports or []:
            args.extend(["-p", port])
        args.append(image)
        if command:
            args.extend(command)
        return self._run(args)

    def run(
        self,
        image: str,
        name: str | None = None,
        networks: list[str] | None = None,
        entrypoint: str | None = None,
        command: list[str] | None = None,
        remove: bool = True,
        check: bool = True,
    ) -> ExecResult:
        """Run a one-off (by default --rm) container, e.g. an ephemeral helper task."""
        args = ["run"]
        if remove:
            args.append("--rm")
        if name:
            args.extend(["--name", name])
        if entrypoint:
            args.extend(["--entrypoint", entrypoint])
        for net in networks or []:
            args.extend(["--network", net])
        args.append(image)
        if command:
            args.extend(command)
        return self._run(args, check=check)

    def destroy(self, name: str) -> ExecResult:
        """Force-remove a container, ignoring errors if it does not exist."""
        return self._run(["rm", "-f", name], check=False)

    def start(self, name: str) -> ExecResult:
        """Start an existing stopped container."""
        return self._run(["start", name])

    def stop(self, name: str) -> ExecResult:
        """Stop a running container."""
        return self._run(["stop", name])

    def exec_command(self, name: str, command: str, check: bool = False) -> ExecResult:
        """Run a shell command inside a running container."""
        return self._run(["exec", name, "sh", "-c", command], check=check)

    def exec_mysql(
        self,
        name: str,
        sql: str,
        user: str = "root",
        password: str = "rootpass",
        database: str | None = None,
        check: bool = True,
    ) -> ExecResult:
        """Run a SQL statement inside a container using the mysql client."""
        args = ["exec", name, "mysql", f"-u{user}", f"-p{password}", "-N", "-B"]
        if database:
            args.extend(["-D", database])
        args.extend(["-e", sql])
        return self._run(args, check=check)

    def exec_mysqlsh(
        self,
        name: str,
        script: str,
        user: str = "root",
        password: str = "rootpass",
        host: str = "localhost",
        port: int = 3306,
        language: str = "js",
        check: bool = True,
    ) -> ExecResult:
        """Run a MySQL Shell (mysqlsh) script inside a container against the given URI."""
        uri = f"{user}:{password}@{host}:{port}"
        args = [
            "exec",
            "-i",
            name,
            "mysqlsh",
            "--no-wizard",
            "--uri",
            uri,
            f"--{language}",
            "-e",
            script,
        ]
        return self._run(args, check=check)

    def network_create(self, name: str) -> ExecResult:
        """Create a container network, reusing it if one with the same name already exists."""
        existing = self._run(
            ["network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
            check=False,
        )
        if existing.ok and existing.stdout.strip() == name:
            return existing
        return self._run(["network", "create", name])

    def network_remove(self, name: str) -> ExecResult:
        """Remove a container network, ignoring errors if it does not exist."""
        return self._run(["network", "rm", name], check=False)

    def volume_remove(self, name: str) -> ExecResult:
        """Remove a container volume, ignoring errors if it does not exist."""
        return self._run(["volume", "rm", name], check=False)

    def container_exists(self, name: str) -> bool:
        """Return True if a container with the exact given name exists (running or stopped)."""
        result = self._run(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            check=False,
        )
        return result.ok and result.stdout.strip() == name
