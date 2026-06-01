import logging
import os
import time

from docker_helper import DockerHelper

_logger = logging.getLogger("GR")


class GroupReplication:
    def __init__(
        self,
        docker: DockerHelper,
        num_nodes: int = 3,
        network: str = "grnet",
        image: str = "percona/percona-server:8.4",
        node_prefix: str = "ps",
        root_password: str = "rootpass",
        base_host_port: int = 33060,
        cluster_name: str = "testCluster",
        group_name: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        gr_port: int = 33061,
        communication_stack: str = "XCOM",
        single_primary: bool = True,
        start_on_boot: bool = True,
        mysql_extra_args: list[str] | None = None,
        verbose: bool | None = None,
    ):
        if num_nodes < 1:
            raise ValueError("num_nodes must be >= 1")
        if verbose is None:
            verbose = os.environ.get("GR_VERBOSE", "").lower() in ("1", "true", "yes", "on")
        self.verbose = verbose
        self.docker = docker
        self.num_nodes = num_nodes
        self.network = network
        self.image = image
        self.node_prefix = node_prefix
        self.root_password = root_password
        self.base_host_port = base_host_port
        self.cluster_name = cluster_name
        self.group_name = group_name
        self.gr_port = gr_port
        self.communication_stack = communication_stack
        self.single_primary = single_primary
        self.start_on_boot = start_on_boot
        self.mysql_extra_args = list(mysql_extra_args or [])
        self.containers: list[str] = []
        self.active_nodes: list[str] = []
        self.networks: list[str] = []

    def log(self, msg: str) -> None:
        if self.verbose:
            _logger.info(msg)

    def _gr_address(self, name: str) -> str:
        return f"{name}:{self.gr_port}"

    def _group_seeds(self) -> str:
        return ",".join(self._gr_address(self._node_name(i)) for i in range(1, self.num_nodes + 1))

    def _node_name(self, index: int) -> str:
        return f"{self.node_prefix}{index}"

    def _volume_name(self, index: int) -> str:
        return f"{self._node_name(index)}-data"

    def _mysqld_args(self, server_id: int, hostname: str) -> list[str]:
        args = [
            f"--server-id={server_id}",
            f"--report-host={hostname}",
            "--log-bin=binlog",
            "--enforce-gtid-consistency=ON",
            "--gtid-mode=ON",
            "--log-replica-updates=ON",
            "--binlog-format=ROW",
            "--plugin-load-add=group_replication.so",
        ]
        args.extend(self.mysql_extra_args)
        return args

    def _wait_ready(self, name: str, timeout: int = 180) -> None:
        self.log(f"wait for {name} to accept connections")
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            result = self.docker.exec_command(
                name, f"mysqladmin -uroot -p{self.root_password} ping --silent"
            )
            if result.ok and "mysqld is alive" in result.stdout:
                return
            last_err = (result.stderr or result.stdout).strip()
            time.sleep(2)
        raise RuntimeError(f"Container {name} did not become ready in {timeout}s. Last: {last_err}")

    def primary(self) -> str:
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        return self.containers[0]

    def get_primary(self, timeout: int = 60) -> str:
        if not self.active_nodes:
            raise RuntimeError("No active nodes")
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            node = self.active_nodes[0]
            result = self.docker.exec_mysql(
                node,
                "SELECT MEMBER_HOST FROM performance_schema.replication_group_members "
                "WHERE MEMBER_ROLE='PRIMARY' AND MEMBER_STATE='ONLINE';",
                check=False,
            )
            host = result.stdout.strip()
            if result.ok and host and "\n" not in host:
                return host
            last = host or (result.stderr or "").strip()
            time.sleep(2)
        raise RuntimeError(f"No PRIMARY elected within {timeout}s (last: {last!r})")

    def stop_node(self, name: str) -> None:
        self.log(f"stop node {name}")
        self.docker.stop(name)
        if name in self.active_nodes:
            self.active_nodes.remove(name)

    def _member_states(self, node: str) -> dict[str, str]:
        result = self.docker.exec_mysql(
            node,
            "SELECT MEMBER_HOST, MEMBER_STATE "
            "FROM performance_schema.replication_group_members;",
            check=False,
        )
        states: dict[str, str] = {}
        if result.ok:
            for line in result.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    states[parts[0]] = parts[1]
        return states

    def wait_all_online(self, timeout: int = 180) -> None:
        self.log("wait for all members ONLINE")
        deadline = time.time() + timeout
        last: dict[str, str] = {}
        while time.time() < deadline:
            states = self._member_states(self.active_nodes[0])
            last = states
            if len(states) == self.num_nodes and all(s == "ONLINE" for s in states.values()):
                return
            time.sleep(2)
        raise RuntimeError(f"Not all members ONLINE within {timeout}s (last: {last})")

    def rejoin_node(self, name: str, timeout: int = 180) -> None:
        self.log(f"start node {name} (auto-rejoin)")
        self.docker.start(name)
        self._wait_ready(name)
        if name not in self.active_nodes:
            self.active_nodes.append(name)
        self.wait_all_online(timeout=timeout)

    def create(self) -> None:
        self.log(f"create network {self.network}")
        self.docker.network_create(self.network)
        self.networks.append(self.network)

        for i in range(1, self.num_nodes + 1):
            name = self._node_name(i)
            self.log(f"start node {name} (server-id={i}, {self.base_host_port + i}->3306)")
            self.docker.create(
                image=self.image,
                name=name,
                hostname=name,
                environment={"MYSQL_ROOT_PASSWORD": self.root_password},
                volumes=[f"{self._volume_name(i)}:/var/lib/mysql"],
                networks=[self.network],
                ports=[f"{self.base_host_port + i}:3306"],
                command=self._mysqld_args(server_id=i, hostname=name),
                restart="always",
            )
            self.containers.append(name)

        self.active_nodes = list(self.containers)

        for name in self.containers:
            self._wait_ready(name)

        primary = self.primary()
        bootstrap_opts = (
            f"groupName:'{self.group_name}',"
            f"communicationStack:'{self.communication_stack}',"
            f"localAddress:'{self._gr_address(primary)}',"
            f"multiPrimary:{'false' if self.single_primary else 'true'}"
        )
        bootstrap_script = (
            f"var c = dba.createCluster('{self.cluster_name}', {{{bootstrap_opts}}});"
        )
        self.log(f"bootstrap cluster on {primary}")
        self.docker.exec_mysqlsh(primary, bootstrap_script)

        for i in range(2, self.num_nodes + 1):
            node = self._node_name(i)
            self.log(f"add {node} to cluster (clone)")
            add_script = (
                f"var c = dba.getCluster('{self.cluster_name}');"
                f"c.addInstance('root:{self.root_password}@{node}:3306', {{"
                f"recoveryMethod:'clone',"
                f"localAddress:'{self._gr_address(node)}'"
                "});"
            )
            self.docker.exec_mysqlsh(primary, add_script)

        status = self.docker.exec_mysqlsh(
            primary,
            f"var c = dba.getCluster('{self.cluster_name}'); print(JSON.stringify(c.status()));",
        )
        if '"status": "ONLINE"' not in status.stdout and '"status":"ONLINE"' not in status.stdout:
            raise RuntimeError(
                f"Cluster did not reach ONLINE state. mysqlsh output:\n{status.stdout}\n{status.stderr}"
            )
        self.log("cluster is ONLINE")

        start_on_boot = "ON" if self.start_on_boot else "OFF"
        seeds = self._group_seeds()
        self.log("persist GR settings (start_on_boot, group_seeds) on each node")
        for name in self.containers:
            self.docker.exec_mysql(
                name,
                f"SET PERSIST group_replication_start_on_boot={start_on_boot};"
                f"SET PERSIST group_replication_group_seeds='{seeds}';",
            )

    def _read_variables(self, name: str, variables: list[str]) -> dict[str, str]:
        in_clause = ",".join(f"'{v}'" for v in variables)
        result = self.docker.exec_mysql(
            name,
            f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({in_clause});",
        )
        actual: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                actual[parts[0]] = parts[1]
        return actual

    def verify(self) -> None:
        if not self.containers:
            raise RuntimeError("Cluster not created yet")

        errors: list[str] = []

        common_expected = {
            "group_replication_group_name": self.group_name,
            "group_replication_start_on_boot": "ON" if self.start_on_boot else "OFF",
            "group_replication_group_seeds": self._group_seeds(),
            "group_replication_single_primary_mode": "ON" if self.single_primary else "OFF",
            "gtid_mode": "ON",
            "enforce_gtid_consistency": "ON",
        }
        variables = list(common_expected.keys()) + ["group_replication_local_address"]

        self.log("verify GR variables on each node")
        for node in self.containers:
            actual = self._read_variables(node, variables)
            for var, expected in common_expected.items():
                got = actual.get(var)
                if got != expected:
                    errors.append(f"{node}: {var}={got!r}, expected {expected!r}")
            expected_local = self._gr_address(node)
            got_local = actual.get("group_replication_local_address")
            if got_local != expected_local:
                errors.append(
                    f"{node}: group_replication_local_address={got_local!r}, "
                    f"expected {expected_local!r}"
                )

        primary = self.primary()
        self.log("check membership via replication_group_members")
        result = self.docker.exec_mysql(
            primary,
            "SELECT MEMBER_HOST, MEMBER_PORT, MEMBER_STATE, MEMBER_ROLE "
            "FROM performance_schema.replication_group_members;",
        )
        members: list[tuple[str, str, str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                members.append((parts[0], parts[1], parts[2], parts[3]))

        if len(members) != self.num_nodes:
            errors.append(
                f"Expected {self.num_nodes} members in replication_group_members, "
                f"got {len(members)}: {members}"
            )

        hosts = sorted(m[0] for m in members)
        expected_hosts = sorted(self.containers)
        if hosts != expected_hosts:
            errors.append(f"Member hosts mismatch: got {hosts}, expected {expected_hosts}")

        bad_state = [m for m in members if m[2] != "ONLINE"]
        if bad_state:
            errors.append(f"Members not ONLINE: {bad_state}")

        primaries = [m for m in members if m[3] == "PRIMARY"]
        secondaries = [m for m in members if m[3] == "SECONDARY"]
        if self.single_primary:
            if len(primaries) != 1:
                errors.append(f"Expected exactly 1 PRIMARY, got {len(primaries)}: {members}")
            if len(secondaries) != self.num_nodes - 1:
                errors.append(
                    f"Expected {self.num_nodes - 1} SECONDARY, got {len(secondaries)}: {members}"
                )

        if errors:
            raise AssertionError("GroupReplication.verify failed:\n  " + "\n  ".join(errors))

    def _list_tables(self, node: str, database: str) -> list[str]:
        result = self.docker.exec_mysql(
            node,
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{database}' AND TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME;",
        )
        return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]

    def _table_checksum(self, node: str, database: str, table: str) -> str:
        result = self.docker.exec_mysql(
            node, f"CHECKSUM TABLE `{database}`.`{table}`;"
        )
        # Output is "<database>.<table>\t<checksum>" (or NULL if the table is missing).
        parts = result.stdout.strip().split("\t")
        return parts[-1] if parts else ""

    def verify_checksums(
        self, database: str, nodes: list[str] | None = None, timeout: int = 30
    ) -> dict[str, str]:
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        nodes = nodes if nodes is not None else self.active_nodes
        if not nodes:
            raise RuntimeError("No nodes to compare")

        self.log(f"list tables in {database}")
        tables = self._list_tables(nodes[0], database)
        if not tables:
            raise AssertionError(f"No base tables found in database {database!r}")

        # Secondaries apply transactions asynchronously, so a checksum taken right
        # after a write can briefly differ. Retry until everything agrees or we time out.
        deadline = time.time() + timeout
        first_pass = True
        while True:
            errors: list[str] = []
            checksums: dict[str, str] = {}
            for table in tables:
                if first_pass:
                    self.log(f"compare checksum {database}.{table} across nodes")
                per_node = {
                    node: self._table_checksum(node, database, table)
                    for node in nodes
                }
                if len(set(per_node.values())) != 1:
                    errors.append(f"{database}.{table} checksum mismatch: {per_node}")
                else:
                    checksums[table] = next(iter(per_node.values()))

            if not errors:
                return checksums
            if time.time() >= deadline:
                raise AssertionError(
                    "GroupReplication.verify_checksums failed:\n  " + "\n  ".join(errors)
                )
            first_pass = False
            time.sleep(1)

    def destroy(self, remove_volumes: bool = False) -> None:
        self.log("destroy cluster")
        for name in reversed(self.containers):
            self.docker.destroy(name)
        if remove_volumes:
            for i in range(1, self.num_nodes + 1):
                self.docker.volume_remove(self._volume_name(i))
        for net in self.networks:
            self.docker.network_remove(net)
        self.containers.clear()
        self.active_nodes.clear()
        self.networks.clear()
