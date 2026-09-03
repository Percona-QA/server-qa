"""Group Replication primary shutdown failover and recovery test.

Loads data via sysbench, takes the current primary's mysqld down to force the election of a
new one, then resumes writes after the failover. Brings the node back, confirms it
auto-rejoins and the cluster is whole again, verifying data stays consistent across all
online nodes (matching checksums) at every stage.

Run twice, for the two ways a node can die. A **stop** is graceful: mysqld shuts down
cleanly and Group Replication announces the member leaving, so the group loses it at once.
A **kill** is abrupt: the process is gone with no goodbye, and the group has to notice a
dead peer by timeout. The recovery path is the same either way, which is the point.

Both leave the process dead. For the variant where mysqld stays alive and only its network
is severed, see test_primary_isolation_failover.py.
"""

import pytest

# Past participle per fault, so assertion messages read as English. Conjugating the
# parameter directly with f"{fault}ed" spells "stoped".
FAULT_PAST = {"stop": "stopped", "kill": "killed"}


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
@pytest.mark.parametrize("fault", ["stop", "kill"])
def test_primary_shutdown_failover_and_recovery(gr_cluster, sysbench, fault):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Take the primary down and confirm a secondary is promoted.
    old_primary = gr_cluster.get_primary()
    if fault == "kill":
        gr_cluster.kill_node(old_primary)
    else:
        gr_cluster.stop_node(old_primary)

    # The remaining two members keep majority; confirm the group settles to exactly
    # 2 ONLINE members (the dead node dropping out) before a new primary is elected. A kill
    # takes longer to register than a graceful stop — the group waits for the peer to time
    # out instead of being told — so the default settle budget matters more here.
    states = gr_cluster.wait_online_count(2)
    online_hosts = [host for host, (state, _) in states.items() if state == "ONLINE"]
    assert old_primary not in online_hosts, (
        f"{FAULT_PAST[fault]} node {old_primary} still ONLINE: {states}"
    )

    new_primary = gr_cluster.get_primary()
    assert new_primary != old_primary, (
        f"primary did not change after {old_primary} was {FAULT_PAST[fault]}"
    )
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

    # Bring the dead node back; it auto-rejoins and the cluster is whole again.
    gr_cluster.rejoin_node(old_primary)
    gr_cluster.wait_all_online()

    # A restarted container can come back on a new IP, and HAProxy resolves its backends
    # once at config-parse time — so rebuild it. Without this the node is health-checked out
    # of the read backend, and anything that later routes writes to it (the next failover,
    # or the next test sharing this cluster) fails to connect.
    gr_cluster.refresh_proxy()
    gr_cluster.wait_proxy_ready(timeout=300)

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the full cluster; data stays consistent across all nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
