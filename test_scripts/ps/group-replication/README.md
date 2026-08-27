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
├── generic_helper.py           # shared SQL/JS string-escaping helpers
├── group_replication_helper.py # GroupReplication — N-node cluster lifecycle
├── sysbench_helper.py          # Sysbench — ephemeral sysbench load container
├── xtrabackup_helper.py        # XtraBackup — full/incremental backup + restore
├── test_basic.py               # smoke test: write on primary, read on every node
├── test_primary_shutdown_failover.py  # primary mysqld stopped: election + auto-rejoin
├── test_scaling.py             # scale up 3->5 and down 5->3 under sysbench load
├── test_backup_restore.py      # XtraBackup full+incremental backup and restore
├── test_secondary_isolation_ist.py # secondary network-partitioned, rejoins via IST
├── test_secondary_isolation_sst.py # same, binlogs purged so it rejoins via clone/SST
└── test_primary_isolation_failover.py # primary partitioned: automatic failover
```

## Prerequisites

- `docker` in `$PATH` (the framework auto-detects `docker` first, then falls
  back to `podman` if `docker` isn't installed). Make sure the Docker daemon
  is running.
- The `percona/percona-server:8.4` image. First run will pull it automatically.
- `mysqlsh` (MySQL Shell) must be available in the chosen `SERVER_IMAGE` (used via `docker exec` for AdminAPI bootstrap).
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

The fixture brings up 3 containers (by default `ps<workerid>-1`, `ps<workerid>-2`, `ps<workerid>-3`) on a per-worker
`grnet-<workerid>` network (e.g. `grnet-0` / `ps0-1..3` when running serially, or `grnet-gw0` / `psgw0-1..3` under pytest-xdist), bootstraps the cluster via mysqlsh, runs the tests,
then removes containers, volumes, and the network. Expect ~1 minute end-to-end.

## Failover test (primary shutdown)

`test_primary_shutdown_failover.py` drives real load and exercises a primary outage:

```bash
GR_VERBOSE=1 pytest -v test_primary_shutdown_failover.py
```

What it does: load initial data with sysbench (`prepare`, 4 tables × 10000 rows),
stop the primary and assert a secondary is promoted, run a 20s `oltp_read_write`
workload against the new primary and compare checksums across the online nodes,
restart the stopped node (it **auto-rejoins** because the framework persists
`group_replication_start_on_boot=ON`), then run another 20s workload against the
full cluster and compare checksums across all three. Expect ~2-3 minutes.

This kills mysqld, so the group loses the member immediately. For the variant where
the process stays alive and only its network is cut, see the partition tests below.

Sysbench notes:
- Runs from the multi-arch image `pingwinator/sysbench:latest` (pulled on first
  use). Each sysbench command is its own one-shot `--rm` container named
  `sysbench_<workerid>_<test-name>` on `grnet-<workerid>` — nothing persists between calls.
- The `sysbench` fixture creates the `sbtest` database and a `sysbench`@`'%'`
  MySQL user (replicated cluster-wide, so it survives failover).
- sysbench targets the cluster's **read/write endpoint**, resolved dynamically
  via `gr_cluster.rw_endpoint()` — the MySQL Router RW port when the router is
  enabled (it routes writes to the current primary and follows failover), or the
  current primary directly otherwise.

Relevant `GroupReplication` helpers: `get_primary()`, `stop_node()`,
`rejoin_node()`, `wait_all_online()`, and `verify_checksums(database, nodes=...)`
(defaults to the currently-online nodes).

## Network partition tests

Three tests injure the cluster with `docker network disconnect` instead of
`docker stop`. That distinction is the whole point: the container keeps running, so
**mysqld stays alive and keeps its data** — the group has to cope with a member it
cannot reach rather than one that has cleanly departed. The node is put back with
`docker network connect`. Expect ~3.5 minutes per proxy for each test.

```bash
GR_VERBOSE=1 pytest -v test_secondary_isolation_ist.py
GR_VERBOSE=1 pytest -v test_secondary_isolation_sst.py
GR_VERBOSE=1 pytest -v test_primary_isolation_failover.py
```

- **`test_secondary_isolation_ist.py`** — isolates one secondary, runs a 30s sysbench
  workload so it falls behind, then heals while the donors still hold the binary logs
  covering that window. Asserts the node came back by **IST**: no new row in
  `performance_schema.clone_status`, and its `gtid_executed` caught up to the primary's.
- **`test_secondary_isolation_sst.py`** — the same partition, but
  `purge_binary_logs()` runs on **every** surviving node before the heal, so no donor can
  serve an IST. Asserts the node came back by **clone/SST**: a *new* clone row with
  `STATE=Completed, ERROR_NO=0`. Purging only the primary would not be enough — GR picks
  its recovery donor from any `ONLINE` member.
- **`test_primary_isolation_failover.py`** — isolates the **primary**. The two surviving
  secondaries hold majority and must expel it, elect a new primary and become writable on
  their own; the test measures and logs that failover window, checks the proxy re-routes
  writes to the new primary, and confirms the old primary rejoins as a `SECONDARY`.
  It sets `group_replication_unreachable_majority_timeout=30` on the primary beforehand:
  at the default of `0` a minority-blocked member never leaves the group, so
  `group_replication_exit_state_action` never fires and writes hang rather than being
  rejected. With the timeout set, the old primary self-ejects and goes `super_read_only`,
  and the test can assert that the write it refused exists on no node afterwards.

Two notes for anyone writing more of these. `performance_schema.clone_status` is never
empty on a secondary — `create()` adds every node with `recoveryMethod:'clone'`, so the
only way to tell an IST from an SST is to compare snapshots taken before and after. And
`COUNT_TRANSACTIONS_REMOTE_APPLIED` stays `0` after a recovery-channel catch-up, since it
only counts what arrives once a member is already `ONLINE`; use `gtid_subset()` instead.

## Scaling test (sysbench)

`test_scaling.py` exercises elastic membership changes and data consistency checks, with a workload phase between scale operations:

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
`verify()`, and `verify_checksums()`. New nodes follow the same naming pattern as the
original members (i.e. `<node_prefix><index>` — e.g. `ps0-4`, `ps0-5`, … with the default fixture).
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

### MySQL Router (`<node_prefix>router`)

The container name is derived from `node_prefix` (e.g. `ps0-router` with the default
fixture, `psrouter` for a class constructed with the default `node_prefix="ps"`).


Image `percona/percona-mysql-router:8.4`, bootstrapped against a live member. Exposes:

- `6446` — classic read/write, routes to the **primary** (follows failover),
- `6447` — classic read-only, load-balances across **secondaries**,
- `6448`/`6449` — the same split over the X protocol.

Host ports `33150` → `6446`, `33151` → `6447` (for manual debugging:
`mysql -h 127.0.0.1 -P 33150 -uroot -prootpass`).

### HAProxy (`<node_prefix>haproxy`)

The container name is derived from `node_prefix` (e.g. `ps0-haproxy` with the default
fixture, `pshaproxy` for a class constructed with the default `node_prefix="ps"`).


Image `percona/haproxy:2`. Two frontends:

- `3307` — read/write, `balance first` → the current **primary**,
- `3308` — read-only, `balance roundrobin` across live members.

HAProxy is not SQL-aware and can't tell which member is the primary. Health checks are
just a plain TCP-connect **liveness** probe (enabled cluster-wide via `default-server
check`); the framework then pins the write backend to the current primary via HAProxy's
**runtime API** over the stats socket (`set server be_write/<node> state ready|maint`) —
the same external-management model the Percona operator uses. On failover,
`wait_proxy_ready()` re-pins the write backend to the newly elected primary.

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
2026-05-27 14:24:09.512 [GR] create network grnet-0
2026-05-27 14:24:09.981 [GR] start node ps0-1 (server-id=1, 33061->3306)
2026-05-27 14:24:11.400 [GR] wait for ps0-1 to accept connections
2026-05-27 14:24:30.210 [GR] bootstrap cluster on ps0-1
2026-05-27 14:24:33.005 [GR] add ps0-2 to cluster (clone)
2026-05-27 14:24:48.117 [GR] add ps0-3 to cluster (clone)
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
    primary = gr_cluster.get_bootstrap_node()
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
docker exec ps0-1 mysqlsh --uri root:rootpass@localhost:3306 --js -e "print(dba.getCluster().status())"

# Quick SQL on the primary
docker exec ps0-1 mysql -uroot -prootpass -e "SELECT @@hostname, @@server_id;"

# Same on a replica
docker exec ps0-2 mysql -uroot -prootpass -e "SELECT * FROM performance_schema.replication_group_members;"

# Interactive mysqlsh session
docker exec -it ps0-1 mysqlsh --uri root:rootpass@localhost:3306

# Tail mysqld error log
docker logs -f ps0-1
```

### Connect from the host

The fixture publishes host ports `33061` (ps0-1), `33062` (ps0-2), `33063` (ps0-3) →
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
docker rm -f <node_prefix>1 <node_prefix>2 <node_prefix>3 <node_prefix>router <node_prefix>haproxy
docker volume rm <node_prefix>1-data <node_prefix>2-data <node_prefix>3-data
docker network rm grnet-<workerid>
```

## Writing more tests

Add files named `test_*.py` in this directory. Request the `gr_cluster`
fixture and use:

- `gr_cluster.exec_sql("SQL;")` — run application SQL through the read/write
  endpoint (the router when enabled, else the primary). Use this for DDL/DML
  instead of targeting a node directly, so the test is proxy-agnostic.
- `gr_cluster.rw_endpoint()` / `gr_cluster.ro_endpoint()` — `(host, port)` for
  read/write or read-only client traffic (e.g. to point sysbench at).
- `gr_cluster.get_bootstrap_node()` — name of the bootstrap node (e.g. `"ps0-1"` when running serially, or `"psgw0-1"` under pytest-xdist); for the
  currently-elected primary (which differs after failover) use `gr_cluster.get_primary()`.
- `gr_cluster.containers` — list of all node names in start order.
- `gr_cluster.stop_node(node)` / `gr_cluster.rejoin_node(node)` — kill and restart a
  node's mysqld; the group sees an immediate member loss.
- `gr_cluster.isolate_node(node)` / `gr_cluster.heal_node(node)` — network-partition a
  node and heal it again. Unlike `stop_node()`, mysqld keeps running and only loses
  connectivity, which is what exercises GR's expulsion, minority-block and distributed
  recovery (IST/SST) paths. `heal_node()` returns `True` when GR rejoined the member on
  its own and `False` when it needed an explicit `START GROUP_REPLICATION`.
- `gr_cluster.node_alive(node)` — does mysqld still answer queries? (liveness, not
  membership — the point of a partition test is that this stays `True`).
- `gr_cluster.local_member_state(node)` — the node's own `MEMBER_STATE` as it sees itself
  (`ONLINE` / `RECOVERING` / `ERROR` / `OFFLINE`), for the minority side of a partition.
- `gr_cluster.wait_node_isolated(node)` — block until an isolated node sees no group
  member but itself as `ONLINE`, and return its view. The minority side runs its own
  suspicion timer, so this is still needed after `wait_online_count()` has settled.
- `gr_cluster.clone_status(node)` — the node's current/last clone operation as a
  column→value map (`ID`, `STATE`, `ERROR_NO`, `BEGIN_TIME`), `{}` if it never cloned.
  Every secondary already carries a completed row from the clone-based `addInstance` in
  `create()`, so to tell an IST from an SST compare snapshots taken before and after,
  rather than checking whether a row exists.
- `gr_cluster.gtid_executed(node)` / `gr_cluster.gtid_subset(node, gtid_set)` — the node's
  `gtid_executed` as a single-line GTID set, and whether it is a superset of another set.
  Use these to prove a node caught up: `COUNT_TRANSACTIONS_REMOTE_APPLIED` stays 0 after a
  recovery-channel catch-up, since it only counts what arrives once a member is `ONLINE`.
- `gr_cluster.purge_binary_logs(nodes=None)` — `FLUSH` + `PURGE BINARY LOGS` on each node
  (default: every active node), returning the resulting `gtid_purged` per node so you can
  assert something was actually purged. Defaults to all nodes because GR picks its recovery
  donor from any `ONLINE` member — purging only the primary still leaves a donor that can
  serve an IST.
- `gr_cluster.docker` — the `DockerHelper`. Common methods:
  - `docker.exec_mysql(node, "SQL;", database=None)` → returns `ExecResult` with `.stdout`, `.stderr`, `.returncode`, `.ok`.
  - `docker.exec_mysqlsh(node, "<JS script>")` — same return shape, runs mysqlsh AdminAPI.
  - `docker.exec_command(node, "shell command")` — arbitrary `sh -c` inside the container.
  - `docker.stop(node)` / `docker.start(node)` — useful for failover-style tests.
  - `docker.network_disconnect(network, node)` / `docker.network_connect(network, node)` —
    the raw partition primitive behind `isolate_node()`/`heal_node()`; both are no-ops when
    the container is already in the requested state. `docker.container_networks(node)`
    reports what it is attached to right now.

Skeleton:

```python
def test_my_thing(gr_cluster):
    primary = gr_cluster.get_bootstrap_node()
    docker = gr_cluster.docker
    docker.exec_mysql(primary, "CREATE DATABASE demo;")
    for node in gr_cluster.containers:
        result = docker.exec_mysql(node, "SHOW DATABASES LIKE 'demo';")
        assert "demo" in result.stdout
```
