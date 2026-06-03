"""Group Replication primary failover and recovery test.

Loads data via sysbench, stops the current primary to force the election of a new one,
and keeps writing through the failover. Then brings the stopped node back, confirms it
auto-rejoins and the cluster is whole again, verifying data stays consistent across all
online nodes (matching checksums) at every stage.
"""


def test_primary_failover_and_recovery(gr_cluster, sysbench):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Stop the primary and confirm a secondary is promoted.
    old_primary = gr_cluster.get_primary()
    gr_cluster.stop_node(old_primary)
    new_primary = gr_cluster.get_primary()
    assert new_primary != old_primary, f"primary did not change after stopping {old_primary}"
    assert new_primary in gr_cluster.active_nodes

    # The read/write endpoint must follow the failover before we load again
    # (the router needs a moment to repoint 6446 at the new primary).
    if gr_cluster.mysql_router:
        gr_cluster._wait_router_ready()

    # Load against the new primary; data stays consistent across the online nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Bring the stopped node back; it auto-rejoins and the cluster is whole again.
    gr_cluster.rejoin_node(old_primary)
    gr_cluster.wait_all_online()
    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the full cluster; data stays consistent across all nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
