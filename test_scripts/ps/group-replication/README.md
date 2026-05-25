# MySQL Group Replication — pytest framework

A small pytest framework that brings up an N-node MySQL Group Replication cluster
in containers (Percona Server 8.4) and runs tests against it.

## Layout

```
group-replication/
├── conftest.py              # gr_cluster fixture (module-scoped)
├── pytest.ini               # python_files = test_*.py
├── requirements.txt         # pytest, pytest-timeout
├── docker_helper.py         # DockerHelper — wraps docker/podman CLI
├── group_replication.py     # GroupReplication — N-node cluster lifecycle
├── test_basic.py            # smoke test: write on primary, read on every node
└── templates/
    └── docker-compose.yaml  # equivalent 3-node topology, for reference
```

## Prerequisites

- `docker` in `$PATH` (the framework auto-detects `docker` first, then falls
  back to `podman` if `docker` isn't installed). Make sure the Docker daemon
  is running.
- The `percona/percona-server:8.4` image. First run will pull it automatically.
- Python venv with `pytest` and `pytest-timeout`:
  ```bash
  /Users/plavi/Development/percona/server-qa/.venv/bin/pip install -r requirements.txt
  ```

## Running the test

From the `group-replication/` directory:

```bash
cd /Users/plavi/Development/percona/server-qa/test_scripts/ps/group-replication
/Users/plavi/Development/percona/server-qa/.venv/bin/pytest -v test_basic.py
```

The fixture brings up 3 containers (`ps1`, `ps2`, `ps3`) on the `grnet` network,
bootstraps the cluster via mysqlsh, runs the tests, then removes containers,
volumes, and the network. Expect ~1 minute end-to-end.

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

### Output verbosity

```bash
pytest -v test_basic.py        # default — verbose
pytest -vv test_basic.py       # extra-verbose: full assertion diffs
pytest -s test_basic.py        # don't capture stdout/stderr (live container logs)
pytest --tb=short test_basic.py  # shorter tracebacks
```

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
docker rm -f ps1 ps2 ps3
docker volume rm ps1-data ps2-data ps3-data
docker network rm grnet
```

## Writing more tests

Add files named `test_*.py` in this directory. Request the `gr_cluster`
fixture and use:

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
