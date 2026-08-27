"""Group Replication primary isolation and automatic failover test.

Scenario 5 of gr_partition_scenarios.md, and the most critical real-world failure path.
The PRIMARY of a 3-node cluster is network-partitioned with `docker network disconnect` —
its mysqld keeps running, it just loses connectivity. The two surviving secondaries hold
majority, so they must detect the loss, expel the old primary, elect a new one and become
writable on their own, with the proxy following the new write endpoint. Meanwhile the
isolated old primary must refuse writes, so no split-brain is possible. After healing it
rejoins as a secondary.

Unlike test_primary_shutdown_failover.py, mysqld is never killed here; the group has to
detect an unreachable member rather than a departed one.

The test sets group_replication_unreachable_majority_timeout=30 on the primary before
isolating it. At the default of 0 a minority-blocked member never leaves the group, so
group_replication_exit_state_action never fires and writes hang instead of being rejected;
with the timeout set, the old primary self-ejects and goes super_read_only as expected.
"""

import time

import pytest

PROBE_TABLE = "sbtest.failover_probe"
# Written to the isolated old primary, which must reject it. Its absence everywhere
# afterwards is the split-brain check.
REJECTED_NOTE = "must-not-commit"


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_primary_isolation_failover(gr_cluster, sysbench):
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

    old_primary = gr_cluster.get_primary()
    exit_action = gr_cluster.docker.exec_mysql(
        old_primary, "SELECT @@GLOBAL.group_replication_exit_state_action;",
        password=gr_cluster.root_password,
    ).stdout.strip()
    gr_cluster.log(f"{old_primary} exit_state_action={exit_action}")

    # Make the primary give up once it finds itself in an unreachable minority. At the
    # default of 0 it would block indefinitely instead of leaving the group, and never
    # apply exit_state_action — see the module docstring.
    gr_cluster.docker.exec_mysql(
        old_primary,
        "SET GLOBAL group_replication_unreachable_majority_timeout=30;",
        password=gr_cluster.root_password,
    )

    # Sever the network only — mysqld on the primary keeps running, unlike stop_node().
    failover_start = time.monotonic()
    gr_cluster.isolate_node(old_primary)

    # The two secondaries keep majority. Wait for the group to settle to exactly 2 ONLINE
    # members before asking who the primary is: for the first few seconds the survivors
    # still report the old primary as the ONLINE PRIMARY, so asking early returns it.
    states = gr_cluster.wait_online_count(2)
    online_hosts = [host for host, (state, _) in states.items() if state == "ONLINE"]
    assert old_primary not in online_hosts, (
        f"isolated primary {old_primary} still ONLINE: {states}"
    )

    # A new primary must have been elected out of the surviving majority, with no operator
    # action of any kind.
    new_primary = gr_cluster.get_primary()
    assert new_primary != old_primary, f"primary did not change after isolating {old_primary}"
    assert new_primary in gr_cluster.active_nodes

    members = gr_cluster.member_states(new_primary)
    online = {host: role for host, (state, role) in members.items() if state == "ONLINE"}
    assert sorted(online.values()) == ["PRIMARY", "SECONDARY"], (
        f"expected one PRIMARY and one SECONDARY among the survivors, got {members}"
    )
    assert online.get(new_primary) == "PRIMARY"

    # Writes resume on the new primary. Timed from the moment of isolation — the number in
    # the log is the interesting part; the assertion is only a sanity ceiling.
    gr_cluster.docker.exec_mysql(
        new_primary,
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('direct-after-failover');",
        password=gr_cluster.root_password,
    )
    failover_seconds = time.monotonic() - failover_start
    gr_cluster.log(
        f"failover window: {failover_seconds:.1f}s from isolating {old_primary} "
        f"to first write on {new_primary}"
    )
    assert failover_seconds < 120, f"failover took {failover_seconds:.1f}s"

    # The whole point of a network partition versus a stop: the process is untouched.
    assert gr_cluster.node_alive(old_primary), f"mysqld on {old_primary} died during the partition"

    # From the minority side the old primary must see that it has lost the group.
    old_view = gr_cluster.wait_node_isolated(old_primary)
    still_online = {h for h, (state, _) in old_view.items() if state == "ONLINE"} - {old_primary}
    assert not still_online, (
        f"isolated primary {old_primary} still sees group members as ONLINE: {old_view}"
    )

    # Having self-ejected, it must protect the data by going read-only...
    assert gr_cluster.wait_super_read_only(old_primary), (
        f"isolated primary {old_primary} is not super_read_only "
        f"(super_read_only={gr_cluster.super_read_only(old_primary)!r}, "
        f"exit_state_action={exit_action!r}, "
        f"member_state={gr_cluster.local_member_state(old_primary)!r})"
    )

    # ...and reject writes. Asserted on failure rather than on error 1290 specifically, so
    # the check holds whether GR rejects the statement or blocks on it.
    rejected = gr_cluster.docker.exec_mysql(
        old_primary,
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{REJECTED_NOTE}');",
        password=gr_cluster.root_password,
        check=False,
        timeout=15,
    )
    assert not rejected.ok, (
        f"isolated primary {old_primary} accepted a write: {rejected.stdout!r}"
    )
    gr_cluster.log(f"{old_primary} rejected the write: {rejected.stderr.strip()!r}")

    # The proxy must follow the election, not just direct connections: exec_sql() goes
    # through the read/write endpoint, so a successful insert here is the assertion.
    # Normally near-instant (~0.2s measured), but MySQL Router intermittently keeps the
    # RW port closed for minutes after the primary is *blackholed* rather than cleanly
    # stopped — the router process stays up (restarts=0), it just has no valid destination.
    # Not reproducible with docker stop, i.e. only on this partition path. Hence the
    # generous timeout; wait_proxy_ready() reports the proxy container's state on failure.
    gr_cluster.wait_proxy_ready(timeout=300)
    gr_cluster.exec_sql(f"INSERT INTO {PROBE_TABLE} (note) VALUES ('proxy-after-failover');")

    # Load against the new primary; the two survivors stay consistent.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Heal. The rejoin may take either the IST or the clone path depending on how much the
    # old primary missed, so the budget covers the slower one and neither is asserted.
    auto_rejoined = gr_cluster.heal_node(old_primary, timeout=240)
    gr_cluster.log(
        f"{old_primary} rejoined "
        f"{'automatically' if auto_rejoined else 'via explicit START GROUP_REPLICATION'}"
    )

    # It comes back as a secondary — the election is not undone by its return.
    members = gr_cluster.member_states(new_primary)
    assert members.get(old_primary) == ("ONLINE", "SECONDARY"), (
        f"{old_primary} did not rejoin as a secondary: {members}"
    )
    assert gr_cluster.get_primary() == new_primary, (
        f"primary moved back off {new_primary} after {old_primary} rejoined"
    )
    # PS 8.4 defaults group_replication_exit_state_action to OFFLINE_MODE, so self-ejecting
    # put the old primary into offline mode (blocking ordinary users, not just writes).
    # Recovering has to clear that, or the node is ONLINE to the group but useless to clients.
    offline_mode = gr_cluster.docker.exec_mysql(
        old_primary, "SELECT @@GLOBAL.offline_mode;", password=gr_cluster.root_password
    ).stdout.strip()
    assert offline_mode == "0", (
        f"{old_primary} is still in offline mode after rejoining (offline_mode={offline_mode!r})"
    )

    # No split-brain: the write the isolated old primary rejected must exist nowhere.
    for node in gr_cluster.active_nodes:
        leaked = gr_cluster.docker.exec_mysql(
            node,
            f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE note='{REJECTED_NOTE}';",
            password=gr_cluster.root_password,
        ).stdout.strip()
        assert leaked == "0", f"rejected write leaked onto {node} ({leaked} rows)"

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the whole cluster again; data stays consistent across all three nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
