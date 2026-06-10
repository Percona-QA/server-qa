# MySQL Group Replication — pytest framework

A small pytest framework that brings up an N-node MySQL Group Replication cluster
in containers (Percona Server 8.4) and runs tests against it.

## Layout

```
group-replication/
├── conftest.py                 # gr_cluster + sysbench + xtrabackup fixtures
├── pytest.ini                  # python_files = test_*.py
├── requirements.txt            # pytest, pytest-timeout
├── docker_helper.py            # DockerHelper — wraps docker/podman CLI
├── group_replication_helper.py # GroupReplication — N-node cluster lifecycle
├── sysbench_helper.py          # Sysbench — ephemeral sysbench load container
├── xtrabackup_helper.py        # XtraBackup — full/incremental backup + restore
├── test_basic.py               # smoke test: write on primary, read on every node
├── test_failover.py            # primary failover + recovery under sysbench load
├── test_scaling.py             # scale up 3->5 and down 5->3 under sysbench load
└── test_backup_restore.py      # XtraBackup full+incremental backup and restore
```

## Prerequisites

- `docker` in `$PATH` (the framework auto-detects `docker` first, then falls
  back to `podman` if `docker` isn't installed). Make sure the Docker daemon
  is running.
- The `percona/percona-server:8.4` image. First run will pull it automatically.
- Python 3.10+ venv with `pytest` and `pytest-timeout`:
  ```bash
  python -m pip install -r requirements.txt
  ```

## Running the test

From the `group-replication/` directory:

```bash
cd test_scripts/ps/group-replication
pytest -v test_basic.py
```

The fixture brings up 3 containers (`ps1`, `ps2`, `ps3`) on the `grnet` network,
bootstraps the cluster via mysqlsh, runs the tests, then removes containers,
volumes, and the network. Expect ~1 minute end-to-end.

## Failover test (sysbench)

`test_failover.py` drives real load and exercises a primary outage:

```bash
GR_VERBOSE=1 pytest -v test_failover.py
```

What it does: load initial data with sysbench (`prepare`, 4 tables × 10000 rows),
stop the primary and assert a secondary is promoted, run a 20s `oltp_read_write`
workload against the new primary and compare checksums across the online nodes,
restart the stopped node (it **auto-rejoins** because the framework persists
`group_replication_start_on_boot=ON`), then run another 20s workload against the
full cluster and compare checksums across all three. Expect ~2-3 minutes.

Sysbench notes:
- Runs from the multi-arch image `pingwinator/sysbench:latest` (pulled on first
  use). Each sysbench command is its own one-shot `--rm` container named
  `sysbench_<test-name>` on `grnet` — nothing persists between calls.
- The `sysbench` fixture creates the `sbtest` database and a `sysbench`@`'%'`
  MySQL user (replicated cluster-wide, so it survives failover).
- sysbench targets the cluster's **read/write endpoint**, resolved dynamically
  via `gr_cluster.rw_endpoint()` — the MySQL Router RW port when the router is
  enabled (it routes writes to the current primary and follows failover), or the
  current primary directly otherwise.

Relevant `GroupReplication` helpers: `get_primary()`, `stop_node()`,
`rejoin_node()`, `wait_all_online()`, and `verify_checksums(database, nodes=...)`
(defaults to the currently-online nodes).

## Scaling test (sysbench)

`test_scaling.py` exercises elastic membership changes under load:

```bash
GR_VERBOSE=1 pytest -v test_scaling.py
```

What it does: load initial data with sysbench, **scale up** the cluster from 3 to 5
nodes (`scale_up(2)` — each new node is started, `addInstance`'d with clone recovery,
and waited ONLINE), verify checksums and cluster configuration across all five nodes,
run a 20s `oltp_read_write` workload, then **scale down** back to 3 (`scale_down(2)` —
removes the most recently added secondaries via `removeInstance`, never the primary,
and destroys their containers/volumes) and re-verify. Expect ~3-4 minutes.

Relevant `GroupReplication` helpers: `scale_up(count)`, `scale_down(count)`,
`verify()`, and `verify_checksums()`. New nodes are named `ps4`, `ps5`, … and the
proxy is reconciled automatically after each change — MySQL Router auto-discovers
members from cluster metadata; HAProxy's container is recreated so its static backend
server list matches the new membership (`_refresh_proxy()`).

## Backup / restore test (XtraBackup)

`test_backup_restore.py` exercises a full + incremental physical backup and restore
with Percona XtraBackup. It runs **behind HAProxy only** (the proxy is irrelevant to
backup/restore):

```bash
GR_VERBOSE=1 pytest -v test_backup_restore.py
```

What it does: load data with sysbench, take a **full** backup of a secondary
(`full_backup` — XtraBackup reads the node's data volume directly while it keeps
serving, using `LOCK INSTANCE FOR BACKUP`), run more load, take an **incremental**
backup (`incremental_backup`) and snapshot the cluster's table checksums at that
point, then run yet more load. It then prepares the chain (`prepare` — base with
`--apply-log-only`, then the incremental merged without it) and restores it
(`copy_back` — `--copy-back` then `chown -R mysql:mysql`) into a fresh **standalone**
node started with group replication off (`start_standalone_node`). Finally it asserts
the restored tables match the incremental-backup-time snapshot (a point-in-time check —
the data is the state *before* the last load), and that the live cluster is untouched
and still healthy. Expect ~4-5 minutes.

XtraBackup notes:
- Uses `percona/percona-xtrabackup:8.4`, a multi-arch image that runs natively on both
  arm64 and amd64 (matching the Percona Server containers — XtraBackup must match the
  server version). Override `image`/`platform` on the `XtraBackup` helper if needed.
- Each XtraBackup step is its own one-shot `--rm` root container; backups live in a
  per-test `grbackup_<test>` volume mounted at `/backup` (`/backup/full`, `/backup/inc1`).
- The `xtrabackup` fixture owns the backup volume and the restore container/volume and
  removes all of them on teardown.

Relevant helpers: `XtraBackup.{full_backup,incremental_backup,prepare,copy_back}`
(`xtrabackup_helper.py`), `GroupReplication.start_standalone_node()` and
`table_checksums()`.

## Proxies (MySQL Router and HAProxy)

Tests run **through a proxy**, chosen per test with an explicit
`@pytest.mark.parametrize("gr_cluster", [...], indirect=True)` so the proxy set is
visible right above the test. Two modes are available — **MySQL Router**
(`"router"`, `mysql_router=True`) and **HAProxy** (`"haproxy"`, `haproxy=True`) —
each yielding a `[router]` / `[haproxy]` id suffix (e.g.
`test_replicates_table_across_nodes[router]`). There is intentionally no "direct"
mode (the `GroupReplication` class still defaults to no proxy for direct use outside
the suite). `create()` starts the proxy on the cluster network **after** the InnoDB
Cluster is ONLINE. The parametrize value is looked up in `PROXIES` in `conftest.py`.

### Choosing which proxies a test runs under

Declare it explicitly on each test (no hidden default — a test without the decorator
gets no `gr_cluster` parameter and fails fast):

```python
# Runs under both proxies
@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_both(gr_cluster): ...

# HAProxy only
@pytest.mark.parametrize("gr_cluster", ["haproxy"], indirect=True)
def test_haproxy_specific(gr_cluster): ...

# MySQL Router only
@pytest.mark.parametrize("gr_cluster", ["router"], indirect=True)
def test_router_specific(gr_cluster): ...
```

### MySQL Router (`psrouter`)

Image `percona/percona-mysql-router:8.4`, bootstrapped against a live member. Exposes:

- `6446` — classic read/write, routes to the **primary** (follows failover),
- `6447` — classic read-only, load-balances across **secondaries**,
- `6448`/`6449` — the same split over the X protocol.

Host ports `33150` → `6446`, `33151` → `6447` (for manual debugging:
`mysql -h 127.0.0.1 -P 33150 -uroot -prootpass`).

### HAProxy (`pshaproxy`)

Image `percona/haproxy:2`. Two frontends:

- `3307` — read/write, `balance first` → the current **primary**,
- `3308` — read-only, `balance roundrobin` across live members.

HAProxy is not SQL-aware and can't tell which member is the primary. Both backends
use the built-in `mysql-check` only for **liveness**; the framework then pins the
write backend to the current primary via HAProxy's **runtime API** over the stats
socket (`set server be_write/<node> state ready|maint`) — the same external-management
model the Percona operator uses. On failover, `wait_proxy_ready()` re-pins the write
backend to the newly elected primary.

Host ports `33152` → `3307`, `33153` → `3308`. The config is injected via an
environment variable (no host bind mounts), and the container runs as the image's
non-root `mysql` user.

### What routes through the proxy and what stays direct

The proxy is only a client connection path for **application traffic** — test
DDL/DML and sysbench go through the read/write endpoint. The control plane stays
**direct, per-node** and never goes through the proxy: the mysqlsh cluster
bootstrap, `SET PERSIST`, `get_primary()`, membership/state polling, per-node
variable checks in `verify()`, and per-node `verify_checksums()` — these inspect
or configure individual members, which a proxy that routes to a single node cannot do.

Proxy-agnostic accessors on `GroupReplication` keep tests identical across proxies:

- `rw_endpoint()` → `(host, port)` for read/write traffic (the proxy's write
  endpoint when enabled, else the live primary on `3306`).
- `ro_endpoint()` → `(host, port)` for read-only traffic (the proxy's read
  endpoint when enabled, else an active secondary on `3306`).
- `exec_sql(sql, database=None)` — run application SQL through the read/write
  endpoint (routed via the proxy when enabled, else direct to the primary).
- `wait_proxy_ready()` — block until the proxy's RW endpoint routes to the current
  primary (used after failover before resuming writes).
- `verify(check_proxy=...)` — defaults to checking, when a proxy is enabled, that
  the RW endpoint routes to the current primary.
- `gr_cluster.proxy` — `"router"`, `"haproxy"`, or `None`.

## Options

### Test selection

```bash
# A single test by id
pytest -v test_basic.py::test_replicates_table_across_nodes

# All tests matching a substring
pytest -v -k replicates

# Only tests marked @pytest.mark.smoke (when you add markers)
pytest -v -m smoke
```

### Container runtime

The framework auto-detects `docker` first, then `podman`, via `shutil.which`.
Override with the `CONTAINER_CLI` env var:

```bash
CONTAINER_CLI=docker pytest -v test_basic.py
CONTAINER_CLI=podman pytest -v test_basic.py
```

> Note: a shell alias such as `docker=podman` does **not** propagate to
> subprocess — the framework looks for an actual binary on `$PATH`. If you
> only have podman installed under that alias, set `CONTAINER_CLI=podman`
> explicitly.

### Overriding container images

Each component's image can be overridden via an environment variable; if unset, the
default below is used. Useful for testing a release candidate, a custom tag, or an
internal registry mirror without editing code.

| Env var            | Component      | Default image                       |
|--------------------|----------------|-------------------------------------|
| `SERVER_IMAGE`     | Percona Server | `percona/percona-server:8.4`        |
| `HAPROXY_IMAGE`    | HAProxy        | `percona/haproxy:2`                 |
| `ROUTER_IMAGE`     | MySQL Router   | `percona/percona-mysql-router:8.4`  |
| `XTRABACKUP_IMAGE` | XtraBackup     | `percona/percona-xtrabackup:8.4`    |
| `SYSBENCH_IMAGE`   | sysbench       | `pingwinator/sysbench:latest`       |

```bash
SERVER_IMAGE=percona/percona-server:8.4.5 pytest -v test_basic.py
```

### Output verbosity

```bash
pytest -v test_basic.py        # default — verbose
pytest -vv test_basic.py       # extra-verbose: full assertion diffs
pytest -s test_basic.py        # don't capture stdout/stderr (live container logs)
pytest --tb=short test_basic.py  # shorter tracebacks
```

### Verbose mode (framework step logging)

By default the framework is silent until something fails. Enable verbose mode to
log a `[GR]` message before each high-level step (create network, start node,
bootstrap, add instance, verify, checksum, destroy) so you can follow a run live.

Enable it with the `GR_VERBOSE` env var:

```bash
GR_VERBOSE=1 pytest -v test_basic.py
```

Sample output (appears in pytest's live log — no `-s` needed; each line is
timestamped to help debug timing/hangs):

```
2026-05-27 14:24:09.512 [GR] create network grnet
2026-05-27 14:24:09.981 [GR] start node ps1 (server-id=1, 33061->3306)
2026-05-27 14:24:11.400 [GR] wait for ps1 to accept connections
2026-05-27 14:24:30.210 [GR] bootstrap cluster on ps1
2026-05-27 14:24:33.005 [GR] add ps2 to cluster (clone)
2026-05-27 14:24:48.117 [GR] add ps3 to cluster (clone)
2026-05-27 14:25:02.640 [GR] cluster is ONLINE
2026-05-27 14:25:02.900 [GR] verify GR variables on each node
2026-05-27 14:25:03.330 [GR] create database gr_test
2026-05-27 14:25:03.560 [GR] create table gr_test.t
2026-05-27 14:25:03.770 [GR] insert 3 rows into gr_test.t
2026-05-27 14:25:04.010 [GR] compare checksum gr_test.t across nodes
2026-05-27 14:25:05.220 [GR] destroy cluster
```

Tests can narrate their own steps the same way by calling `gr_cluster.log("...")`
— it shares the `[GR]` prefix and obeys the same `GR_VERBOSE` toggle (see the
`gr_cluster.log(...)` calls in `test_basic.py`).

When constructing the cluster directly you can also pass `verbose=True`:

```python
cluster = GroupReplication(helper, num_nodes=3, verbose=True)
```

Outside pytest (e.g. a standalone script), configure logging so the messages
show: `logging.basicConfig(level=logging.INFO)`.

### Timeouts

`pytest-timeout` is installed. To cap a single run:

```bash
pytest --timeout=300 test_basic.py
```

Or add `@pytest.mark.timeout(300)` to individual tests.

### Cluster size and other knobs

The default fixture creates 3 nodes. To customize per-test, write your own
fixture using the framework classes directly:

```python
@pytest.fixture(scope="module")
def big_cluster():
    helper = DockerHelper()
    cluster = GroupReplication(
        helper,
        num_nodes=5,
        network="bignet",
        node_prefix="big",
        base_host_port=33070,        # → host ports 33071..33075
        cluster_name="bigCluster",
        mysql_extra_args=["--innodb-buffer-pool-size=256M"],
    )
    cluster.create()
    try:
        yield cluster
    finally:
        cluster.destroy(remove_volumes=True)
```

## Debugging

### Drop into pdb on failure

```bash
pytest -v --pdb test_basic.py
```

pytest will pause at the failing assertion. Useful commands inside pdb:
`l` (list code), `p <expr>` (print), `c` (continue), `q` (quit).

### Pause with `breakpoint()` mid-test

Insert `breakpoint()` anywhere in the test or in `conftest.py` to stop there:

```python
def test_replicates_table_across_nodes(gr_cluster):
    primary = gr_cluster.primary()
    docker = gr_cluster.docker
    breakpoint()       # <-- stops here; cluster is fully up
    docker.exec_mysql(primary, "CREATE DATABASE IF NOT EXISTS gr_test;")
```

For the breakpoint prompt to be interactive you must disable pytest's output
capture:

```bash
pytest -v -s test_basic.py
```

### Inspect a live cluster from another shell

While paused at a `breakpoint()` (or while the fixture is up), open another
terminal and use `docker exec`:

```bash
# Cluster status
docker exec ps1 mysqlsh --uri root:rootpass@localhost:3306 --js -e "print(dba.getCluster().status())"

# Quick SQL on the primary
docker exec ps1 mysql -uroot -prootpass -e "SELECT @@hostname, @@server_id;"

# Same on a replica
docker exec ps2 mysql -uroot -prootpass -e "SELECT * FROM performance_schema.replication_group_members;"

# Interactive mysqlsh session
docker exec -it ps1 mysqlsh --uri root:rootpass@localhost:3306

# Tail mysqld error log
docker logs -f ps1
```

### Connect from the host

The fixture publishes host ports `33061` (ps1), `33062` (ps2), `33063` (ps3) →
container `3306`. So while the cluster is up:

```bash
mysql -h 127.0.0.1 -P 33061 -uroot -prootpass
mysqlsh --uri root:rootpass@127.0.0.1:33061
```

(Useful with GUI clients too: MySQL Workbench, DBeaver, TablePlus.)

### Keep the cluster up after a test

The easiest way is to keep `breakpoint()` paused — the fixture only tears
down once the test returns. Alternatively, comment out the
`cluster.destroy(remove_volumes=True)` line in `conftest.py` for a one-off
manual session, then clean up yourself (see below).

### Re-run after a crashed setup

If a previous run aborted before the fixture's teardown, you'll have leftover
containers/network/volumes. Clean them up:

```bash
docker rm -f ps1 ps2 ps3 psrouter pshaproxy
docker volume rm ps1-data ps2-data ps3-data
docker network rm grnet
```

## Writing more tests

Add files named `test_*.py` in this directory. Request the `gr_cluster`
fixture and use:

- `gr_cluster.exec_sql("SQL;")` — run application SQL through the read/write
  endpoint (the router when enabled, else the primary). Use this for DDL/DML
  instead of targeting a node directly, so the test is proxy-agnostic.
- `gr_cluster.rw_endpoint()` / `gr_cluster.ro_endpoint()` — `(host, port)` for
  read/write or read-only client traffic (e.g. to point sysbench at).
- `gr_cluster.primary()` — name of the bootstrap node (`"ps1"`).
- `gr_cluster.containers` — list of all node names in start order.
- `gr_cluster.docker` — the `DockerHelper`. Common methods:
  - `docker.exec_mysql(node, "SQL;", database=None)` → returns `ExecResult` with `.stdout`, `.stderr`, `.returncode`, `.ok`.
  - `docker.exec_mysqlsh(node, "<JS script>")` — same return shape, runs mysqlsh AdminAPI.
  - `docker.exec_command(node, "shell command")` — arbitrary `sh -c` inside the container.
  - `docker.stop(node)` / `docker.start(node)` — useful for failover-style tests.

Skeleton:

```python
def test_my_thing(gr_cluster):
    primary = gr_cluster.primary()
    docker = gr_cluster.docker
    docker.exec_mysql(primary, "CREATE DATABASE demo;")
    for node in gr_cluster.containers:
        result = docker.exec_mysql(node, "SHOW DATABASES LIKE 'demo';")
        assert "demo" in result.stdout
```
