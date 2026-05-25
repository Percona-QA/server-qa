import pytest

from docker_helper import DockerHelper
from group_replication import GroupReplication


@pytest.fixture(scope="module")
def gr_cluster():
    helper = DockerHelper()
    cluster = GroupReplication(helper, num_nodes=3)
    cluster.create()
    try:
        yield cluster
    finally:
        cluster.destroy(remove_volumes=True)
