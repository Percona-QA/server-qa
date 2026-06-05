import re

import pytest

from docker_helper import DockerHelper
from group_replication_helper import GroupReplication
from sysbench_helper import Sysbench


# Proxy modes the suite can run a test behind. There is intentionally no "direct"
# entry: every test runs behind a proxy. Each test selects its proxies explicitly
# with @pytest.mark.parametrize("gr_cluster", [...], indirect=True) — see the test
# files. The value passed (e.g. "router"/"haproxy") is the key looked up here.
PROXIES = {
    "router": {"mysql_router": True},
    "haproxy": {"haproxy": True},
}


@pytest.fixture(scope="module")
def gr_cluster(request):
    # request.param is supplied by each test's @pytest.mark.parametrize(..., indirect=True).
    helper = DockerHelper()
    cluster = GroupReplication(helper, num_nodes=3, **PROXIES[request.param])
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
    # test node name would otherwise be rejected, so sanitize it.
    safe_node = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
    name = f"sysbench_{safe_node}"
    sb = Sysbench(gr_cluster.docker, network=gr_cluster.network, name=name, log=gr_cluster.log)
    gr_cluster.exec_sql(
        f"CREATE DATABASE IF NOT EXISTS {sb.database};"
        f"CREATE USER IF NOT EXISTS '{sb.mysql_user}'@'%' IDENTIFIED BY '{sb.mysql_password}';"
        f"GRANT ALL ON {sb.database}.* TO '{sb.mysql_user}'@'%';",
    )
    try:
        yield sb
    finally:
        gr_cluster.docker.destroy(name)
