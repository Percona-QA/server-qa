from collections.abc import Callable

from docker_helper import DockerHelper


class Sysbench:
    def __init__(
        self,
        docker: DockerHelper,
        network: str,
        name: str,
        image: str = "pingwinator/sysbench:latest",
        mysql_user: str = "sysbench",
        mysql_password: str = "sysbench",
        database: str = "sbtest",
        tables: int = 4,
        table_size: int = 10000,
        threads: int = 4,
        log: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.network = network
        self.name = name
        self.image = image
        self.mysql_user = mysql_user
        self.mysql_password = mysql_password
        self.database = database
        self.tables = tables
        self.table_size = table_size
        self.threads = threads
        self.log = log or (lambda msg: None)

    def _args(self, host: str, workload: str, port: int = 3306) -> list[str]:
        return [
            workload,
            "--db-driver=mysql",
            f"--mysql-host={host}",
            f"--mysql-port={port}",
            f"--mysql-user={self.mysql_user}",
            f"--mysql-password={self.mysql_password}",
            f"--mysql-db={self.database}",
            f"--tables={self.tables}",
            f"--table-size={self.table_size}",
        ]

    def _exec(self, command: list[str]):
        return self.docker.run(
            self.image,
            name=self.name,
            networks=[self.network],
            entrypoint="sysbench",
            command=command,
            remove=True,
            check=True,
        )

    def prepare(self, host: str, workload: str = "oltp_read_write", port: int = 3306):
        self.log(f"sysbench prepare ({self.tables} tables x {self.table_size} rows) on {host}:{port}")
        return self._exec(self._args(host, workload, port) + ["prepare"])

    def run(self, host: str, workload: str = "oltp_read_write", time: int = 20, port: int = 3306):
        self.log(f"sysbench {workload} run for {time}s ({self.threads} threads) on {host}:{port}")
        return self._exec(
            self._args(host, workload, port)
            + [f"--threads={self.threads}", f"--time={time}", "--report-interval=5", "run"]
        )
