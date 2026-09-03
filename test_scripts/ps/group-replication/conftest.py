import os
import re
import sys
from types import SimpleNamespace

import pytest

# The helper modules below are imported as top-level modules. This directory can't be a
# package (the 'group-replication' hyphen is not a valid identifier), so add it to
# sys.path explicitly — otherwise importing them fails when pytest is invoked from a
# different working directory (e.g. the repo root in CI).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docker_helper import DockerHelper  # noqa: E402
from generic_helper import sql_ident, sql_str  # noqa: E402
from group_replication_helper import GroupReplication  # noqa: E402
from sysbench_helper import Sysbench  # noqa: E402
from xtrabackup_helper import XtraBackup  # noqa: E402


# Proxy modes the suite can run a test behind. There is intentionally no "direct"
# entry: every test runs behind a proxy. Each test selects its proxies explicitly
# with @pytest.mark.parametrize("gr_cluster", [...], indirect=True) — see the test
# files. The value passed is either a proxy name ("router"/"haproxy", giving the
# default 3 nodes) or a (proxy, num_nodes) tuple for a differently-sized cluster;
# the proxy name is the key looked up here.
PROXIES = {
    "router": {"mysql_router": True},
    "haproxy": {"haproxy": True},
}


def _worker_id(request) -> str:
    """Return the pytest-xdist worker id (e.g. 'gw0'), or '0' when running serially.

    Used to make Docker container/volume names unique per worker — names are global on
    the Docker host, so resources derived only from the test id collide across workers
    running the same test under pytest-xdist.
    """
    return getattr(request.config, "workerinput", {}).get("workerid", "0")


@pytest.fixture(scope="module")
def gr_cluster(request):
    # request.param is the proxy, supplied by each test's
    # @pytest.mark.parametrize("gr_cluster", [...], indirect=True). Validate it explicitly
    # so a test that forgets the decorator fails with a clear message instead of an opaque
    # AttributeError (no param) / KeyError (unknown proxy).
    #
    # A test needing a different cluster size passes a (proxy, num_nodes) tuple instead of a
    # bare proxy name — wrapped in pytest.param(..., id=proxy), or the node id degrades to
    # "gr_cluster0". Everything else about the fixture is the same either way.
    param = getattr(request, "param", None)
    if param is None:
        raise pytest.UsageError(
            'gr_cluster requires a proxy via indirect parametrization, e.g. '
            '@pytest.mark.parametrize("gr_cluster", ["haproxy"], indirect=True)'
        )
    if isinstance(param, tuple):
        if len(param) != 2:
            raise pytest.UsageError(
                f"gr_cluster tuple parameter must be (proxy, num_nodes); got {param!r}"
            )
        proxy, num_nodes = param
    else:
        proxy, num_nodes = param, 3
    # isinstance before the lookup: an unhashable proxy (e.g. a list) would otherwise raise
    # TypeError from inside the dict membership test rather than reporting the bad value.
    if not isinstance(proxy, str) or proxy not in PROXIES:
        raise pytest.UsageError(
            f"unknown gr_cluster proxy {proxy!r}; valid options: {sorted(PROXIES)}"
        )
    # bool is a subclass of int, so without the isinstance guard True would pass as 1 node.
    if isinstance(num_nodes, bool) or not isinstance(num_nodes, int) or num_nodes < 1:
        raise pytest.UsageError(
            f"gr_cluster num_nodes must be a positive integer; got {num_nodes!r}"
        )
    try:
        helper = DockerHelper()
    except RuntimeError as exc:
        # No docker/podman on PATH: skip rather than error the whole run (e.g. when the
        # suite is collected in an environment without a container runtime).
        pytest.skip(f"no container runtime available: {exc}")
    workerid = _worker_id(request)
    # Use the full worker id (sanitized to [a-zA-Z0-9]) for the globally-unique resource
    # names: deriving node_prefix from offset alone collapses "0" (serial) and "gw0"
    # (xdist) to the same "ps0-" prefix, which would clash across execution modes. offset
    # (the numeric suffix) is used only to give concurrent xdist workers distinct host
    # port ranges.
    safe_workerid = re.sub(r"[^a-zA-Z0-9]", "", workerid) or "0"
    m = re.search(r"\d+$", workerid)
    offset = int(m.group()) if m else 0
    cluster = GroupReplication(
        helper,
        num_nodes=num_nodes,
        network=f"grnet-{safe_workerid}",
        node_prefix=f"ps{safe_workerid}-",
        base_host_port=33060 + offset * 100,
        **PROXIES[proxy],
    )
    # The cluster is bootstrapped via mysqlsh's AdminAPI; skip rather than fail the whole
    # suite when the server image ships without MySQL Shell. Checked before create() so no
    # nodes are started when it's missing.
    if not cluster.mysqlsh_available():
        pytest.skip(f"mysqlsh not available in server image {cluster.server_image!r}")
    try:
        # create() is inside the try so a partially-built cluster (e.g. a failed
        # proxy bring-up) is still torn down instead of leaking containers.
        cluster.create()
        yield cluster
    finally:
        cluster.destroy(remove_volumes=True)


@pytest.fixture
def sysbench(request, gr_cluster):
    # Container names allow only [a-zA-Z0-9_.-]; the parametrized "[router]" suffix in the
    # test node name would otherwise be rejected, so sanitize it. The worker id keeps the
    # name unique across parallel pytest-xdist workers running the same test.
    safe_node = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
    name = f"sysbench_{_worker_id(request)}_{safe_node}"
    sb = Sysbench(gr_cluster.docker, network=gr_cluster.network, name=name, log=gr_cluster.log)
    db = sql_ident(sb.database)
    user = sql_str(sb.mysql_user)
    password = sql_str(sb.mysql_password)
    gr_cluster.exec_sql(
        f"CREATE DATABASE IF NOT EXISTS {db};"
        f"CREATE USER IF NOT EXISTS {user}@'%' IDENTIFIED BY {password};"
        f"GRANT ALL ON {db}.* TO {user}@'%';",
    )
    try:
        yield sb
    finally:
        # Drop the tables so a later test sharing this module-scoped cluster can prepare()
        # again — prepare() creates them outright and fails if they already exist. check=False
        # and the broad except: a cluster left unhealthy by a failing test must not turn
        # teardown into a second error that masks the real one.
        try:
            cleanup_host, cleanup_port = gr_cluster.rw_endpoint()
            sb.cleanup(host=cleanup_host, port=cleanup_port, check=False)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask a test failure
            gr_cluster.log(f"sysbench cleanup skipped: {exc}")
        gr_cluster.docker.destroy(name)


@pytest.fixture
def xtrabackup(request, gr_cluster):
    # Per-test resource names (container names allow only [a-zA-Z0-9_.-]). The worker id
    # keeps every container/volume unique across parallel pytest-xdist workers running the
    # same test — these names are global on the Docker host.
    safe_node = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
    prefix = f"{_worker_id(request)}_{safe_node}"
    backup_volume = f"grbackup_{prefix}"
    restore_container = f"psrestore_{prefix}"
    restore_volume = f"{restore_container}-data"
    helper = XtraBackup(
        gr_cluster.docker,
        network=gr_cluster.network,
        backup_volume=backup_volume,
        root_password=gr_cluster.root_password,
        name_prefix=f"xtrabackup_{prefix}",
        log=gr_cluster.log,
    )
    bundle = SimpleNamespace(
        helper=helper,
        restore_container=restore_container,
        restore_volume=restore_volume,
    )
    try:
        yield bundle
    finally:
        gr_cluster.docker.destroy(restore_container)
        gr_cluster.docker.volume_remove(restore_volume)
        helper.cleanup()
