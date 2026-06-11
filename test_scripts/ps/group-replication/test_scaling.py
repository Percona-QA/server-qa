"""Group Replication scale up / scale down test.

Starts from a 3-node cluster, loads data via sysbench, scales the cluster up to 5
nodes, runs a workload, then scales it back down to 3. Confirms membership and cluster
configuration are correct after each change and that data stays consistent (matching
checksums) across all online nodes at every stage, behind both proxy backends.
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_scale_up_and_down(gr_cluster, sysbench):
    # Healthy 3-node cluster to start from.
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Scale up by 2 (3 -> 5 nodes); the freshly cloned secondaries must catch up.
    gr_cluster.scale_up(2)
    gr_cluster.verify()
    assert gr_cluster.num_nodes == 5
    gr_cluster.verify_checksums("sbtest", timeout=180)

    # Run an OLTP read/write workload against the grown cluster; data stays consistent.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Scale down by 2 (5 -> 3 nodes); the cluster configuration is correct afterwards.
    gr_cluster.scale_down(2)
    gr_cluster.verify()
    assert gr_cluster.num_nodes == 3
    gr_cluster.verify_checksums("sbtest", timeout=120)
