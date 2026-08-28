"""Group Replication majority loss and quorum failure tolerance test.

Scenario 1 of gr_partition_scenarios.md. Both secondaries of a 3-node cluster are
network-partitioned at the same time with `docker network disconnect` — their mysqld keeps
running, they just lose connectivity. That leaves every member alone in a one-node
partition, so no side holds a majority and the cluster must accept no writes at all: the
surviving primary goes read-only rather than risk diverging. Reconnecting the secondaries
restores a 2-of-3 quorum between them, after which the old primary rejoins.

Unlike the other partition tests, which always leave a writable majority behind, this one
removes the majority itself. It asserts that writes are refused both directly on the
primary and through the proxy, and that neither of those writes exists anywhere afterwards.

The test sets group_replication_unreachable_majority_timeout=30 on the primary only. At the
default of 0 the primary would block indefinitely instead of leaving the group, never
applying group_replication_exit_state_action and so never becoming super_read_only. It is
deliberately not set cluster-wide: the secondaries have to stay in the group while blocked,
otherwise nobody holds quorum after the heal and the group would need a manual rebuild.
"""

import pytest

PROBE_TABLE = "sbtest.quorum_probe"
# Written while the cluster has no quorum. Both must be refused, and neither may exist
# anywhere afterwards.
REJECTED_NOTES = ("rejected-direct", "rejected-proxy")


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_majority_loss(gr_cluster, sysbench):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)

    # A probe table for the write attempts below. It lives in the sbtest database so the
    # verify_checksums("sbtest") calls already in this test cover the probe rows too.
    # GR requires a primary key on every table.
    gr_cluster.exec_sql(
        f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} "
        "(id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(64));"
    )
    gr_cluster.verify_checksums("sbtest", timeout=120)

    primary = gr_cluster.get_primary()
    secondaries = gr_cluster.secondaries()
    exit_action = gr_cluster.docker.exec_mysql(
        primary, "SELECT @@GLOBAL.group_replication_exit_state_action;",
        password=gr_cluster.root_password,
    ).stdout.strip()
    gr_cluster.log(f"{primary} exit_state_action={exit_action}")

    # Make the primary give up once it finds itself in an unreachable minority — see the
    # module docstring for why this is set here and not on the whole cluster.
    gr_cluster.docker.exec_mysql(
        primary,
        "SET GLOBAL group_replication_unreachable_majority_timeout=30;",
        password=gr_cluster.root_password,
    )

    # Cut off both secondaries at once. Every mysqld stays running.
    gr_cluster.partition_group(secondaries)

    # Assert this first: it has to land inside the ~30s before the primary leaves the group
    # and stops listing the others at all. Detection takes a few seconds, so there is room.
    view = gr_cluster.wait_members_unreachable(secondaries, node=primary)
    unreachable = [h for h, (state, _) in view.items() if state == "UNREACHABLE"]
    assert sorted(unreachable) == sorted(secondaries), (
        f"expected {secondaries} UNREACHABLE from {primary}, got {view}"
    )

    # The whole point of a network partition versus a stop: the processes are untouched.
    for node in secondaries:
        assert gr_cluster.node_alive(node), f"mysqld on {node} died during the partition"

    # Neither isolated node can see anyone else either, so no side holds a majority. This is
    # what separates majority loss from the single-secondary isolation tests.
    for node in secondaries:
        node_view = gr_cluster.wait_node_isolated(node)
        still_online = {h for h, (state, _) in node_view.items() if state == "ONLINE"} - {node}
        assert not still_online, (
            f"isolated node {node} still sees group members as ONLINE: {node_view}"
        )

    # Having lost majority, the surviving primary must protect the data by going read-only
    # rather than accepting writes it can never replicate.
    assert gr_cluster.wait_super_read_only(primary), (
        f"surviving primary {primary} is not super_read_only "
        f"(super_read_only={gr_cluster.super_read_only(primary)!r}, "
        f"exit_state_action={exit_action!r}, "
        f"member_state={gr_cluster.local_member_state(primary)!r})"
    )

    # Writes must be refused directly on the primary (expected error 1290). Asserted on
    # failure rather than the error code, matching the sibling partition tests.
    rejected = gr_cluster.docker.exec_mysql(
        primary,
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{REJECTED_NOTES[0]}');",
        password=gr_cluster.root_password,
        check=False,
        timeout=15,
    )
    assert not rejected.ok, f"primary {primary} accepted a write with no quorum: {rejected.stdout!r}"
    gr_cluster.log(f"{primary} rejected the direct write: {rejected.stderr.strip()!r}")

    # ...and through the proxy. Deliberately after the read-only check above: until the
    # primary leaves the group GR *blocks* writes rather than rejecting them, so probing
    # earlier would hang instead of failing. The two proxies refuse for different reasons
    # (HAProxy forwards to the read-only primary; the router has no primary to route to),
    # so the assertion is on failure, not on a particular error.
    via_proxy = gr_cluster.exec_sql(
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{REJECTED_NOTES[1]}');",
        check=False,
        timeout=30,
    )
    assert not via_proxy.ok, (
        f"{gr_cluster.proxy} accepted a write with no quorum: {via_proxy.stdout!r}"
    )
    gr_cluster.log(f"{gr_cluster.proxy} rejected the write: {via_proxy.stderr.strip()!r}")

    # Reconnect the secondaries. Restoring the network is not enough on its own: a member
    # blocked in a minority stays stuck with the others UNREACHABLE rather than reforming,
    # so the surviving membership has to be forced onto one of them. This is the documented
    # recovery from majority loss, and it is the step the scenario doc assumes happens by
    # itself — it does not.
    gr_cluster.heal_group(secondaries)

    # Forced down to a single member, not to both secondaries: XCOM refuses a forced list
    # containing anyone it currently suspects, and each blocked node still suspects the
    # others even after the network is back ("Only alive members in the current
    # configuration should be present in a forced configuration list").
    seed = secondaries[0]
    survivors = gr_cluster.force_members([seed], node=seed)
    online = {h for h, (state, _) in survivors.items() if state == "ONLINE"}
    assert online == {seed}, f"forced membership did not settle on {seed}: {survivors}"

    # Everyone else rejoins the reformed group. The remaining secondary is still stuck in
    # its stale blocked view, and the old primary left the group entirely when its
    # unreachable-majority timeout fired; both need Group Replication restarted.
    for node in (secondaries[1], primary):
        gr_cluster.restart_group_replication(node)
    gr_cluster.wait_all_online(timeout=240, node=seed)

    # The cluster is whole again with exactly one primary. Which node holds it is not
    # asserted: the survivors elected one of themselves while the old primary was out.
    new_primary = gr_cluster.get_primary()
    members = gr_cluster.member_states(new_primary)
    assert sorted(members) == sorted(gr_cluster.containers), f"membership incomplete: {members}"
    assert all(state == "ONLINE" for state, _ in members.values()), f"not all ONLINE: {members}"
    assert sorted(role for _, role in members.values()) == ["PRIMARY", "SECONDARY", "SECONDARY"], (
        f"expected exactly one PRIMARY, got {members}"
    )

    # PS 8.4 defaults group_replication_exit_state_action to OFFLINE_MODE, so leaving the
    # group put the old primary into offline mode (blocking ordinary users, not just writes).
    # Recovering has to clear that, or the node is ONLINE to the group but useless to clients.
    offline_mode = gr_cluster.docker.exec_mysql(
        primary, "SELECT @@GLOBAL.offline_mode;", password=gr_cluster.root_password
    ).stdout.strip()
    assert offline_mode == "0", (
        f"{primary} is still in offline mode after rejoining (offline_mode={offline_mode!r})"
    )

    # The write endpoint has to come back too. Reconnecting a container to the network
    # gives it a new IP, and HAProxy resolved its backends once at start time — so after a
    # heal it is pointing at addresses nothing answers on and has to be rebuilt. (Only this
    # test notices: it is the first where a node that was reconnected goes on to become the
    # primary.) The generous timeout is for the same reason as in
    # test_primary_isolation_failover.py: MySQL Router can be slow to reopen its RW port.
    gr_cluster.refresh_proxy()
    gr_cluster.wait_proxy_ready(timeout=300)

    # No data was accepted anywhere while quorum was lost.
    notes = ", ".join(f"'{note}'" for note in REJECTED_NOTES)
    for node in gr_cluster.active_nodes:
        leaked = gr_cluster.docker.exec_mysql(
            node,
            f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE note IN ({notes});",
            password=gr_cluster.root_password,
        ).stdout.strip()
        assert leaked == "0", f"a rejected write leaked onto {node} ({leaked} rows)"

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the recovered cluster; data stays consistent across all three nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
