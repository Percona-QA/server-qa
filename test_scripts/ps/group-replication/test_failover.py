"""Group Replication primary failover and recovery test.

Loads data via sysbench, stops the current primary to force the election of a new one,
then resumes writes after the failover. Brings the stopped node back, confirms it
auto-rejoins and the cluster is whole again, verifying data stays consistent across all
online nodes (matching checksums) at every stage.
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_primary_failover_and_recovery(gr_cluster, sysbench):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Stop the primary and confirm a secondary is promoted.
    old_primary = gr_cluster.get_primary()
    gr_cluster.stop_node(old_primary)

    # The remaining two members keep majority; confirm the group settles to exactly
    # 2 ONLINE members (the stopped node dropping out) before a new primary is elected.
    states = gr_cluster.wait_online_count(2)
    online_hosts = [host for host, (state, _) in states.items() if state == "ONLINE"]
    assert old_primary not in online_hosts, f"stopped node {old_primary} still ONLINE: {states}"

    new_primary = gr_cluster.get_primary()
    assert new_primary != old_primary, f"primary did not change after stopping {old_primary}"
    assert new_primary in gr_cluster.active_nodes

    # The two surviving members must be exactly one PRIMARY (the newly elected one)
    # and one SECONDARY.
    members = gr_cluster.member_states(new_primary)
    online = {host: role for host, (state, role) in members.items() if state == "ONLINE"}
    assert sorted(online.values()) == ["PRIMARY", "SECONDARY"], (
        f"expected one PRIMARY and one SECONDARY, got {members}"
    )
    assert online.get(new_primary) == "PRIMARY"

    # The read/write endpoint must follow the failover before we load again
    # (the proxy needs a moment to repoint at the new primary).
    if gr_cluster.proxy:
        gr_cluster.wait_proxy_ready()

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
