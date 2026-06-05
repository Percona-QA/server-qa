import re

import pytest

from docker_helper import DockerHelper
from group_replication_helper import GroupReplication
from sysbench_helper import Sysbench


# Proxy modes the suite exercises in front of the cluster. There is intentionally
# no "direct" entry: every test runs behind a proxy. By default a test runs under
# all of them; restrict with @pytest.mark.proxies("haproxy") / ("router").
PROXIES = {
    "router": {"mysql_router": True},
    "haproxy": {"haproxy": True},
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "proxies(*names): restrict a test to a subset of proxy modes "
        "(e.g. @pytest.mark.proxies('haproxy')); unmarked runs under all.",
    )


def pytest_generate_tests(metafunc):
    if "gr_cluster" not in metafunc.fixturenames:
        return
    marker = metafunc.definition.get_closest_marker("proxies")
    names = list(marker.args) if marker else list(PROXIES)
    unknown = [n for n in names if n not in PROXIES]
    if unknown:
        raise pytest.UsageError(f"unknown proxies marker value(s): {unknown}")
    metafunc.parametrize("gr_cluster", names, indirect=True, ids=names, scope="module")


@pytest.fixture(scope="module")
def gr_cluster(request):
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
