"""Group Replication rejoin into a group that changed while the node was away.

Ticket case 3. A secondary is killed, a fourth node is added to the cluster while it is
down, load runs so the group's GTID state moves on, and only then is the killed node
brought back. It has to rejoin a group that is both a different size and further ahead than
the one it left — which is the part the other failover tests do not cover, since there the
membership is unchanged by the time the node returns.

Growing the cluster with a member down is why scale_up() persists settings only on the
nodes that are up; rejoin_nodes() then refreshes the returning node's seed list, without
which verify() would fail it for still advertising the old three-node membership.
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_rejoin_after_scale_up(gr_cluster, sysbench):
    gr_cluster.verify()
    assert gr_cluster.num_nodes == 3

    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Kill a secondary. The remaining two keep majority, so the group carries on without it.
    victim = gr_cluster.secondaries()[0]
    gr_cluster.kill_node(victim)
    states = gr_cluster.wait_online_count(2)
    online_hosts = [h for h, (state, _) in states.items() if state == "ONLINE"]
    assert victim not in online_hosts, f"killed node {victim} still ONLINE: {states}"

    # Grow the cluster while it is down: the group it eventually rejoins is a different one.
    added = gr_cluster.scale_up(1)
    assert gr_cluster.num_nodes == 4
    assert len(gr_cluster.active_nodes) == 3, (
        f"the killed node should still be out: {gr_cluster.active_nodes}"
    )
    gr_cluster.log(f"added {', '.join(added)} while {victim} was down")

    # Move the group's GTID state on, so the returning node has real catching up to do
    # rather than finding everything exactly as it left it.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", nodes=gr_cluster.active_nodes, timeout=120)

    # Bring it back. It rejoins a four-member group that is ahead of where it stopped.
    gr_cluster.rejoin_node(victim, timeout=300)

    members = gr_cluster.member_states(gr_cluster.get_primary())
    assert sorted(members) == sorted(gr_cluster.containers), f"membership incomplete: {members}"
    assert all(state == "ONLINE" for state, _ in members.values()), f"not all ONLINE: {members}"
    assert members.get(victim) == ("ONLINE", "SECONDARY"), (
        f"{victim} did not rejoin as a secondary: {members}"
    )
    assert sorted(role for _, role in members.values()).count("PRIMARY") == 1, (
        f"expected exactly one PRIMARY across the four nodes, got {members}"
    )

    # The restarted container can come back on a new IP, and HAProxy resolves its backends
    # once at start time, so rebuild it before relying on the endpoint again.
    gr_cluster.refresh_proxy()
    gr_cluster.wait_proxy_ready(timeout=300)

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=180)

    # Load against the reunited four-node cluster; data stays consistent everywhere.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=180)
