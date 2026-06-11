import json
import logging
import os
import shlex
import time
from urllib.parse import quote

from docker_helper import DockerHelper

_logger = logging.getLogger("GR")


class GroupReplication:
    def __init__(
        self,
        docker: DockerHelper,
        num_nodes: int = 3,
        network: str = "grnet",
        server_image: str | None = None,
        node_prefix: str = "ps",
        root_password: str = "rootpass",
        base_host_port: int = 33060,
        cluster_name: str = "testCluster",
        group_name: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        gr_port: int = 33061,
        communication_stack: str = "XCOM",
        single_primary: bool = True,
        start_on_boot: bool = True,
        mysql_router: bool = False,
        router_image: str | None = None,
        router_rw_port: int = 6446,
        router_ro_port: int = 6447,
        haproxy: bool = False,
        haproxy_image: str | None = None,
        haproxy_write_port: int = 3307,
        haproxy_read_port: int = 3308,
        mysql_extra_args: list[str] | None = None,
        verbose: bool | None = None,
    ):
        if num_nodes < 1:
            raise ValueError("num_nodes must be >= 1")
        if mysql_router and haproxy:
            raise ValueError("mysql_router and haproxy are mutually exclusive")
        if verbose is None:
            verbose = os.environ.get("GR_VERBOSE", "").lower() in ("1", "true", "yes", "on")
        self.verbose = verbose
        self.docker = docker
        self.num_nodes = num_nodes
        self.network = network
        self.server_image = server_image or os.environ.get("SERVER_IMAGE") or "percona/percona-server:8.4"
        self.node_prefix = node_prefix
        self.root_password = root_password
        self.base_host_port = base_host_port
        self.cluster_name = cluster_name
        self.group_name = group_name
        self.gr_port = gr_port
        self.communication_stack = communication_stack
        self.single_primary = single_primary
        self.start_on_boot = start_on_boot
        self.mysql_router = mysql_router
        self.router_image = router_image or os.environ.get("ROUTER_IMAGE") or "percona/percona-mysql-router:8.4"
        self.router_name = f"{node_prefix}router"
        self.router_rw_port = router_rw_port
        self.router_ro_port = router_ro_port
        self.haproxy = haproxy
        self.haproxy_image = haproxy_image or os.environ.get("HAPROXY_IMAGE") or "percona/haproxy:2"
        self.haproxy_name = f"{node_prefix}haproxy"
        self.haproxy_write_port = haproxy_write_port
        self.haproxy_read_port = haproxy_read_port
        # The active proxy in front of the cluster, if any.
        self.proxy = "router" if mysql_router else "haproxy" if haproxy else None
        self._proxy_started = False
        self.mysql_extra_args = list(mysql_extra_args or [])
        self.containers: list[str] = []
        self.active_nodes: list[str] = []
        self.node_index: dict[str, int] = {}
        self.networks: list[str] = []

    def log(self, msg: str) -> None:
        """Log a message, but only when verbose mode is enabled."""
        if self.verbose:
            _logger.info(msg)

    @property
    def proxy_name(self) -> str | None:
        """Container name of the active proxy (router/haproxy), or None when running direct."""
        if self.proxy == "router":
            return self.router_name
        if self.proxy == "haproxy":
            return self.haproxy_name
        return None

    def _gr_address(self, name: str) -> str:
        """Build the GR communication address (host:gr_port) for a node."""
        return f"{name}:{self.gr_port}"

    @staticmethod
    def _js_str(value: str) -> str:
        """Encode a Python string as a JavaScript string literal for mysqlsh --js scripts.

        JSON encoding yields a valid JS string literal with quotes, backslashes, and
        control/non-ASCII characters escaped, so values like cluster_name or the
        connection URI (which contains root_password) can't break the script or inject.
        """
        return json.dumps(value)

    def _instance_uri(self, node: str) -> str:
        """Build the AdminAPI connection URI for a node, percent-encoding the credentials.

        Even though the URI is embedded as an escaped JS string, mysqlsh then parses it
        as a connection string, so a password with URI-reserved characters (@ : / # ?)
        must be encoded or AdminAPI misparses it.
        """
        return f"root:{quote(self.root_password, safe='')}@{node}:3306"

    def _add_instance_script(self, node: str) -> str:
        """Build the mysqlsh AdminAPI script to add a node to the cluster (clone recovery)."""
        return (
            f"var c = dba.getCluster({self._js_str(self.cluster_name)});"
            f"c.addInstance({self._js_str(self._instance_uri(node))}, {{"
            f"recoveryMethod:'clone',"
            f"localAddress:{self._js_str(self._gr_address(node))}"
            "});"
        )

    def _group_seeds(self) -> str:
        """Build the comma-separated group_replication_group_seeds list for all current nodes."""
        return ",".join(self._gr_address(name) for name in self.containers)

    def _node_name(self, index: int) -> str:
        """Build the container/host name for the node at the given 1-based index."""
        return f"{self.node_prefix}{index}"

    def _volume_name(self, index: int) -> str:
        """Build the data volume name for the node at the given 1-based index."""
        return f"{self._node_name(index)}-data"

    def _mysqld_args(self, server_id: int, hostname: str) -> list[str]:
        """Build the mysqld command-line arguments required for Group Replication on a node."""
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
        """Wait until a node accepts MySQL connections (runs a trivial query), or time out."""
        self.log(f"wait for {name} to accept connections")
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            # Probe via exec_mysql (no shell) rather than mysqladmin via sh -c, so a
            # root_password with shell metacharacters/spaces can't break the loop or be
            # mangled by shell parsing.
            result = self.docker.exec_mysql(
                name, "SELECT 1;", password=self.root_password, check=False, timeout=15
            )
            if result.ok:
                return
            last_err = (result.stderr or result.stdout).strip()
            time.sleep(2)
        raise RuntimeError(f"Container {name} did not become ready in {timeout}s. Last: {last_err}")

    def get_bootstrap_node(self) -> str:
        """Return the bootstrap node name (the first container created).

        This is a fixed node, distinct from get_primary(), which queries the cluster for
        the currently-elected ONLINE primary (they differ after a failover).
        """
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        return self.containers[0]

    def get_primary(self, timeout: int = 60) -> str:
        """Query the cluster for the host of the currently elected ONLINE PRIMARY, or time out."""
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
                password=self.root_password,
                check=False,
            )
            host = result.stdout.strip()
            if result.ok and host and "\n" not in host:
                return host
            last = host or (result.stderr or "").strip()
            time.sleep(2)
        raise RuntimeError(f"No PRIMARY elected within {timeout}s (last: {last!r})")

    def stop_node(self, name: str) -> None:
        """Stop a node's container and remove it from the active-nodes list."""
        self.log(f"stop node {name}")
        self.docker.stop(name)
        if name in self.active_nodes:
            self.active_nodes.remove(name)

    def _member_states(self, node: str) -> dict[str, str]:
        """Read the host->state map of all group members as seen from the given node."""
        result = self.docker.exec_mysql(
            node,
            "SELECT MEMBER_HOST, MEMBER_STATE "
            "FROM performance_schema.replication_group_members;",
            password=self.root_password,
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
        """Wait until every expected member reports the ONLINE state, or time out."""
        if not self.active_nodes:
            raise RuntimeError("No active nodes")
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
        """Restart a stopped node and wait for it to auto-rejoin and all members to be ONLINE."""
        self.log(f"start node {name} (auto-rejoin)")
        self.docker.start(name)
        self._wait_ready(name)
        if name not in self.active_nodes:
            self.active_nodes.append(name)
        self.wait_all_online(timeout=timeout)

    def _persist_gr_settings(self, nodes: list[str]) -> None:
        """Persist start_on_boot and the current group_seeds list on each given node."""
        start_on_boot = "ON" if self.start_on_boot else "OFF"
        seeds = self._group_seeds()
        for name in nodes:
            self.docker.exec_mysql(
                name,
                f"SET PERSIST group_replication_start_on_boot={start_on_boot};"
                f"SET PERSIST group_replication_group_seeds='{seeds}';",
                password=self.root_password,
            )

    def scale_up(self, count: int = 1) -> list[str]:
        """Grow the cluster by adding count new instances, then wait for everything ONLINE.

        Mirrors create()'s add path: start a fresh mysqld container, addInstance it via
        mysqlsh (clone recovery), persist the updated seed list on all nodes, and reconcile
        the proxy. Returns the names of the added nodes.
        """
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        if count < 1:
            raise ValueError("count must be >= 1")
        primary = self.get_primary()
        added: list[str] = []
        next_index = max(self.node_index.values())
        for _ in range(count):
            next_index += 1
            node = self._start_mysqld_node(next_index)
            self._wait_ready(node)
            self.log(f"add {node} to cluster (clone)")
            self.docker.exec_mysqlsh(
                primary, self._add_instance_script(node), password=self.root_password
            )
            self.active_nodes.append(node)
            added.append(node)

        self.num_nodes = len(self.containers)
        self.log("persist GR settings (start_on_boot, group_seeds) on each node")
        self._persist_gr_settings(self.containers)
        self.wait_all_online()
        if self.proxy:
            self._refresh_proxy()
            self.wait_proxy_ready()
        return added

    def scale_down(self, count: int = 1) -> list[str]:
        """Shrink the cluster by removing count secondaries, then wait for everything ONLINE.

        Never removes the current primary. Removed nodes are taken from the most recently
        added secondaries, removed from the InnoDB Cluster via mysqlsh, then their container
        and data volume are destroyed. The seed list is re-persisted and the proxy reconciled.
        Returns the names of the removed nodes.
        """
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        if count < 1:
            raise ValueError("count must be >= 1")
        primary = self.get_primary()
        removable = [n for n in self.active_nodes if n != primary]
        if count > len(removable):
            raise ValueError(
                f"Cannot remove {count} nodes: only {len(removable)} removable secondaries"
            )
        # Remove most-recently-added secondaries first.
        to_remove = list(reversed(removable))[:count]
        for node in to_remove:
            self.log(f"remove {node} from cluster")
            remove_script = (
                f"var c = dba.getCluster({self._js_str(self.cluster_name)});"
                f"c.removeInstance({self._js_str(self._instance_uri(node))});"
            )
            self.docker.exec_mysqlsh(primary, remove_script, password=self.root_password)
            self.docker.destroy(node)
            self.docker.volume_remove(self._volume_name(self.node_index[node]))
            self.containers.remove(node)
            self.active_nodes.remove(node)
            del self.node_index[node]

        self.num_nodes = len(self.containers)
        self.log("persist GR settings (start_on_boot, group_seeds) on each node")
        self._persist_gr_settings(self.containers)
        if self.proxy:
            self._refresh_proxy()
            self.wait_proxy_ready()
        self.wait_all_online()
        return to_remove

    def rw_endpoint(self) -> tuple[str, int]:
        """Return the (host, port) for read/write traffic: the proxy when enabled, else the live primary."""
        if self.proxy == "router":
            return (self.router_name, self.router_rw_port)
        if self.proxy == "haproxy":
            return (self.haproxy_name, self.haproxy_write_port)
        return (self.get_primary(), 3306)

    def secondaries(self) -> list[str]:
        """Return the active secondary node names (every active node except the current primary)."""
        primary = self.get_primary()
        return [n for n in self.active_nodes if n != primary]

    def ro_endpoint(self) -> tuple[str, int]:
        """Return the (host, port) for read-only traffic: the proxy when enabled, else an active secondary."""
        if self.proxy == "router":
            return (self.router_name, self.router_ro_port)
        if self.proxy == "haproxy":
            return (self.haproxy_name, self.haproxy_read_port)
        secondaries = self.secondaries()
        return ((secondaries[0] if secondaries else self.get_primary()), 3306)

    def exec_sql(self, sql: str, database: str | None = None, check: bool = True):
        """Run application SQL through the read/write endpoint (via the proxy when enabled, else direct to the primary)."""
        if self.proxy:
            host, port = self.rw_endpoint()
            return self.docker.exec_mysql(
                self.active_nodes[0],
                sql,
                password=self.root_password,
                database=database,
                host=host,
                port=port,
                check=check,
            )
        return self.docker.exec_mysql(
            self.get_primary(),
            sql,
            password=self.root_password,
            database=database,
            check=check,
        )

    def _start_router(self) -> None:
        """Start a MySQL Router container, bootstrapping it against the live InnoDB Cluster.

        We override the image entrypoint and bootstrap explicitly: the Percona router image's
        own (non-Kubernetes) entrypoint binds the routing ports to localhost, which is
        unreachable from other containers. --conf-bind-address=0.0.0.0 fixes that, and we run
        as the image's non-root user (no --user privilege drop).
        """
        seed = self.active_nodes[0]
        self.log(f"start MySQL Router {self.router_name} (bootstrap from {seed})")
        # _instance_uri percent-encodes the credentials so mysqlrouter parses the URI
        # correctly; shlex.quote then protects the surrounding bash -c command (it chains
        # bootstrap && exec) from a password with spaces/$/quotes/etc.
        bootstrap_uri = shlex.quote(self._instance_uri(seed))
        bootstrap = (
            f"mysqlrouter --bootstrap {bootstrap_uri} "
            "--directory /tmp/mysqlrouter "
            "--conf-set-option=DEFAULT.unknown_config_option=warning "
            "--conf-bind-address=0.0.0.0 --force "
            "&& exec mysqlrouter -c /tmp/mysqlrouter/mysqlrouter.conf"
        )
        self.docker.create(
            image=self.router_image,
            name=self.router_name,
            hostname=self.router_name,
            network=self.network,
            ports=[
                f"{self.base_host_port + 90}:{self.router_rw_port}",
                f"{self.base_host_port + 91}:{self.router_ro_port}",
            ],
            entrypoint="bash",
            command=["-c", bootstrap],
            restart="on-failure",
        )

    def _haproxy_config(self) -> str:
        """Build the haproxy.cfg: write frontend (:3307) to the primary, read frontend (:3308) round-robin.

        HAProxy is not SQL-aware and cannot tell which member is the primary, so both backends use the
        built-in mysql-check only for liveness (no forking external-check, which is pathologically slow
        under the amd64-on-arm64 emulation this operator image runs in). The write backend lists every
        node but the framework keeps only the current primary in the ready state via the runtime API
        (see _haproxy_set_write_primary) — the same external-management model the Percona operator uses.
        """
        servers = "\n".join(f"  server {n} {n}:3306" for n in self.containers)
        return (
            "global\n"
            "  maxconn 2048\n"
            "  log stdout format raw local0\n"
            "  stats socket /tmp/haproxy.sock mode 660 level admin\n"
            "\n"
            "defaults\n"
            "  mode tcp\n"
            "  log global\n"
            "  option tcplog\n"
            "  timeout connect 5s\n"
            "  timeout queue 10s\n"
            "  timeout client 30m\n"
            "  timeout server 30m\n"
            "  default-server check inter 2s rise 1 fall 3\n"
            "\n"
            "backend be_write\n"
            "  balance first\n"
            "  option mysql-check\n"
            f"{servers}\n"
            "\n"
            "backend be_read\n"
            "  balance roundrobin\n"
            "  option mysql-check\n"
            f"{servers}\n"
            "\n"
            "frontend fe_write\n"
            f"  bind :{self.haproxy_write_port}\n"
            "  default_backend be_write\n"
            "\n"
            "frontend fe_read\n"
            f"  bind :{self.haproxy_read_port}\n"
            "  default_backend be_read\n"
        )

    def _haproxy_set_write_primary(self, primary: str) -> None:
        """Pin the write backend to the given primary: mark it ready and every other node in maintenance.

        Uses HAProxy's runtime API over the stats socket, so writes only reach the primary even though
        mysql-check alone cannot distinguish it. Errors are ignored (e.g. before the socket is up).
        """
        # HAProxy's runtime API is line-oriented: one command per line, each terminated
        # by a newline. Joining with ';' would be sent as a single command and ignored.
        cmds = "".join(
            f"set server be_write/{n} state {'ready' if n == primary else 'maint'}\n"
            for n in self.containers
        )
        self.docker.exec_command(
            self.haproxy_name,
            f"printf '%s' '{cmds}' | socat - UNIX-CONNECT:/tmp/haproxy.sock",
            check=False,
        )

    def _start_haproxy(self) -> None:
        """Start a Percona HAProxy container fronting the cluster (write :3307, read :3308).

        The config is injected via an environment variable (avoids host bind mounts, unreliable under
        podman-on-macOS) and written to /tmp before haproxy is exec'd in the foreground as the image's
        non-root user.
        """
        self.log(f"start HAProxy {self.haproxy_name} (write:{self.haproxy_write_port} read:{self.haproxy_read_port})")
        command = (
            'printf "%s\\n" "$HAPROXY_CFG" > /tmp/haproxy.cfg && '
            "exec haproxy -W -db -f /tmp/haproxy.cfg"
        )
        self.docker.create(
            image=self.haproxy_image,
            name=self.haproxy_name,
            hostname=self.haproxy_name,
            environment={"HAPROXY_CFG": self._haproxy_config()},
            network=self.network,
            ports=[
                f"{self.base_host_port + 92}:{self.haproxy_write_port}",
                f"{self.base_host_port + 93}:{self.haproxy_read_port}",
            ],
            entrypoint="bash",
            command=["-c", command],
            restart="on-failure",
        )

    def _start_proxy(self) -> None:
        """Start whichever proxy (router or haproxy) is configured in front of the cluster."""
        if self.proxy == "router":
            self._start_router()
        elif self.proxy == "haproxy":
            self._start_haproxy()

    def _refresh_proxy(self) -> None:
        """Reconcile the proxy with the current membership after a scale operation.

        MySQL Router auto-discovers members from the cluster metadata, so it needs nothing.
        HAProxy's backend server list is baked into the config at start time (see
        _haproxy_config), so the container is recreated to pick up the new node set.
        """
        if self.proxy == "haproxy":
            self.log(f"refresh HAProxy {self.haproxy_name} for new membership")
            self.docker.destroy(self.haproxy_name)
            self._start_haproxy()

    def wait_proxy_ready(self, timeout: int = 120) -> None:
        """Wait until the proxy accepts connections and routes the read/write endpoint to the current primary.

        The default is generous because HAProxy's external health checks can take tens of seconds to
        first stabilize when the operator image runs under CPU emulation.
        """
        if not self.proxy:
            return
        host, port = self.rw_endpoint()
        self.log(f"wait for {self.proxy} {self.proxy_name} to route to the primary")
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            # HAProxy can't detect the primary itself; (re-)pin the write backend to the
            # current primary each iteration. Idempotent, and self-heals after failover.
            if self.proxy == "haproxy":
                self._haproxy_set_write_primary(self.get_primary())
            result = self.docker.exec_mysql(
                self.active_nodes[0],
                "SELECT @@hostname;",
                password=self.root_password,
                host=host,
                port=port,
                check=False,
                timeout=15,
            )
            routed = result.stdout.strip()
            if result.ok and routed and routed == self.get_primary():
                return
            last = routed or (result.stderr or "").strip()
            time.sleep(2)
        raise RuntimeError(
            f"{self.proxy} {self.proxy_name} not ready / not routing to primary in {timeout}s (last: {last!r})"
        )

    def _start_mysqld_node(self, index: int) -> str:
        """Create and start a single mysqld container for the node at the given index.

        Records the node in self.containers and self.node_index and returns its name.
        Does not add it to the InnoDB Cluster (the caller bootstraps or addInstance).
        """
        name = self._node_name(index)
        self.log(f"start node {name} (server-id={index}, {self.base_host_port + index}->3306)")
        self.docker.create(
            image=self.server_image,
            name=name,
            hostname=name,
            environment={"MYSQL_ROOT_PASSWORD": self.root_password},
            volumes=[f"{self._volume_name(index)}:/var/lib/mysql"],
            network=self.network,
            ports=[f"{self.base_host_port + index}:3306"],
            command=self._mysqld_args(server_id=index, hostname=name),
            restart="always",
        )
        self.containers.append(name)
        self.node_index[name] = index
        return name

    def start_standalone_node(self, name: str, data_volume: str, server_id: int = 99) -> str:
        """Start a standalone PS container on a pre-populated (e.g. restored) data volume.

        The node is NOT attached to the cluster network and group replication is kept off
        (--group-replication-start-on-boot=OFF overrides the persisted ON), so it never
        contacts the live group. The GR plugin is still loaded, so the datadir's persisted
        group_replication_* variables remain valid. Useful for inspecting restored data in
        isolation. Returns the container name; it is not tracked in self.containers.
        """
        self.log(f"start standalone node {name} (restored data, GR off)")
        self.docker.create(
            image=self.server_image,
            name=name,
            hostname=name,
            volumes=[f"{data_volume}:/var/lib/mysql"],
            command=self._mysqld_args(server_id=server_id, hostname=name)
            + ["--group-replication-start-on-boot=OFF"],
            restart="on-failure",
        )
        self._wait_ready(name)
        return name

    def create(self) -> None:
        """Create the network and nodes, bootstrap the cluster, add instances, and persist GR settings."""
        self.log(f"create network {self.network}")
        self.docker.network_create(self.network)
        self.networks.append(self.network)

        for i in range(1, self.num_nodes + 1):
            self._start_mysqld_node(i)

        self.active_nodes = list(self.containers)

        for name in self.containers:
            self._wait_ready(name)

        bootstrap_node = self.get_bootstrap_node()
        bootstrap_opts = (
            f"groupName:{self._js_str(self.group_name)},"
            f"communicationStack:{self._js_str(self.communication_stack)},"
            f"localAddress:{self._js_str(self._gr_address(bootstrap_node))},"
            f"multiPrimary:{'false' if self.single_primary else 'true'}"
        )
        bootstrap_script = (
            f"var c = dba.createCluster({self._js_str(self.cluster_name)}, {{{bootstrap_opts}}});"
        )
        self.log(f"bootstrap cluster on {bootstrap_node}")
        self.docker.exec_mysqlsh(bootstrap_node, bootstrap_script, password=self.root_password)
        for i in range(2, self.num_nodes + 1):
            node = self._node_name(i)
            self.log(f"add {node} to cluster (clone)")
            self.docker.exec_mysqlsh(
                bootstrap_node, self._add_instance_script(node), password=self.root_password
            )

        status = self.docker.exec_mysqlsh(
            bootstrap_node,
            f"var c = dba.getCluster({self._js_str(self.cluster_name)}); "
            "print(JSON.stringify(c.status()));",
            password=self.root_password,
        )
        if '"status": "ONLINE"' not in status.stdout and '"status":"ONLINE"' not in status.stdout:
            raise RuntimeError(
                f"Cluster did not reach ONLINE state. mysqlsh output:\n{status.stdout}\n{status.stderr}"
            )
        self.log("cluster is ONLINE")

        self.log("persist GR settings (start_on_boot, group_seeds) on each node")
        self._persist_gr_settings(self.containers)

        if self.proxy:
            self._start_proxy()
            self._proxy_started = True
            self.wait_proxy_ready()

    def _read_variables(self, name: str, variables: list[str]) -> dict[str, str]:
        """Read the given global variables from a node as a name->value map.

        Uses check=False so a transient failure (node down/restarting) yields an empty
        map rather than raising — verify() then records the missing values as structured
        mismatches instead of crashing before it can report the collected errors.
        """
        in_clause = ",".join(f"'{v}'" for v in variables)
        result = self.docker.exec_mysql(
            name,
            f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({in_clause});",
            password=self.root_password,
            check=False,
        )
        actual: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                actual[parts[0]] = parts[1]
        return actual

    def verify(self, check_proxy: bool | None = None) -> None:
        """Assert that GR variables, membership, states, and roles match the expected configuration.

        When check_proxy is set (defaults to whether a proxy is enabled), also assert that the
        proxy's read/write endpoint routes to the current primary.
        """
        if not self.containers:
            raise RuntimeError("Cluster not created yet")
        if check_proxy is None:
            check_proxy = self.proxy is not None

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

        if not self.active_nodes:
            raise RuntimeError("No active nodes to query membership")
        # Query a currently-active node, not self.get_bootstrap_node(), which may be
        # intentionally stopped during failover testing while other members are ONLINE.
        query_node = self.active_nodes[0]
        self.log("check membership via replication_group_members")
        result = self.docker.exec_mysql(
            query_node,
            "SELECT MEMBER_HOST, MEMBER_PORT, MEMBER_STATE, MEMBER_ROLE "
            "FROM performance_schema.replication_group_members;",
            password=self.root_password,
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
        primary_hosts = ", ".join(sorted(m[0] for m in primaries)) or "none"
        secondary_hosts = ", ".join(sorted(m[0] for m in secondaries)) or "none"
        self.log(f"primary: {primary_hosts}; secondaries: {secondary_hosts}")
        if self.single_primary:
            if len(primaries) != 1:
                errors.append(f"Expected exactly 1 PRIMARY, got {len(primaries)}: {members}")
            if len(secondaries) != self.num_nodes - 1:
                errors.append(
                    f"Expected {self.num_nodes - 1} SECONDARY, got {len(secondaries)}: {members}"
                )

        if check_proxy and self.proxy:
            self.log(f"verify {self.proxy} routes read/write traffic to the primary")
            host, port = self.rw_endpoint()
            result = self.docker.exec_mysql(
                self.active_nodes[0],
                "SELECT @@hostname;",
                password=self.root_password,
                host=host,
                port=port,
                check=False,
            )
            routed = result.stdout.strip()
            if not result.ok or routed != self.get_primary():
                errors.append(
                    f"{self.proxy} RW endpoint routes to {routed!r}, expected primary {self.get_primary()!r}"
                )

        if errors:
            raise AssertionError("GroupReplication.verify failed:\n  " + "\n  ".join(errors))

    def _list_tables(self, node: str, database: str) -> list[str]:
        """List the base table names in a database on the given node."""
        result = self.docker.exec_mysql(
            node,
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{database}' AND TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME;",
            password=self.root_password,
        )
        return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]

    def _table_checksum(self, node: str, database: str, table: str) -> str:
        """Return the CHECKSUM TABLE value for a single table on the given node."""
        result = self.docker.exec_mysql(
            node, f"CHECKSUM TABLE `{database}`.`{table}`;", password=self.root_password
        )
        # Output is "<database>.<table>\t<checksum>" (or NULL if the table is missing).
        parts = result.stdout.strip().split("\t")
        return parts[-1] if parts else ""

    def table_checksums(self, node: str, database: str) -> dict[str, str]:
        """Return the CHECKSUM TABLE value of every base table in a database on one node."""
        tables = self._list_tables(node, database)
        if not tables:
            raise AssertionError(f"No base tables found in database {database!r} on {node}")
        return {table: self._table_checksum(node, database, table) for table in tables}

    def verify_checksums(
        self, database: str, nodes: list[str] | None = None, timeout: int = 30
    ) -> dict[str, str]:
        """Compare per-table checksums across nodes (retrying for replication lag) and return them."""
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
        """Remove all containers, optionally their data volumes, and the networks, then reset state."""
        self.log("destroy cluster")
        if self.proxy_name and (self._proxy_started or self.docker.container_exists(self.proxy_name)):
            self.log(f"remove {self.proxy} {self.proxy_name}")
            self.docker.destroy(self.proxy_name)
            self._proxy_started = False
        volumes = [self._volume_name(self.node_index[name]) for name in self.containers]
        for name in reversed(self.containers):
            self.docker.destroy(name)
        if remove_volumes:
            for volume in volumes:
                self.docker.volume_remove(volume)
        for net in self.networks:
            self.docker.network_remove(net)
        self.containers.clear()
        self.active_nodes.clear()
        self.node_index.clear()
        self.networks.clear()
