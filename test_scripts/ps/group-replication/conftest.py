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
# files. The value passed (e.g. "router"/"haproxy") is the key looked up here.
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
    # request.param is supplied by each test's @pytest.mark.parametrize(..., indirect=True).
    helper = DockerHelper()
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
        num_nodes=3,
        network=f"grnet-{safe_workerid}",
        node_prefix=f"ps{safe_workerid}-",
        base_host_port=33060 + offset * 100,
        **PROXIES[request.param],
    )
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
