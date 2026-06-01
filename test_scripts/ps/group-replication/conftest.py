import pytest

from docker_helper import DockerHelper
from group_replication_helper import GroupReplication
from sysbench_helper import Sysbench


@pytest.fixture(scope="module")
def gr_cluster():
    helper = DockerHelper()
    cluster = GroupReplication(helper, num_nodes=3)
    cluster.create()
    try:
        yield cluster
    finally:
        cluster.destroy(remove_volumes=True)


@pytest.fixture
def sysbench(request, gr_cluster):
    name = f"sysbench_{request.node.name}"
    sb = Sysbench(gr_cluster.docker, network=gr_cluster.network, name=name, log=gr_cluster.log)
    gr_cluster.docker.exec_mysql(
        gr_cluster.get_primary(),
        f"CREATE DATABASE IF NOT EXISTS {sb.database};"
        f"CREATE USER IF NOT EXISTS '{sb.mysql_user}'@'%' IDENTIFIED BY '{sb.mysql_password}';"
        f"GRANT ALL ON {sb.database}.* TO '{sb.mysql_user}'@'%';",
    )
    try:
        yield sb
    finally:
        gr_cluster.docker.destroy(name)
