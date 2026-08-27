"""Group Replication minority node isolation with IST recovery test.

Scenario 2 of gr_partition_scenarios.md. A single secondary of a 3-node cluster is
network-partitioned with `docker network disconnect` — its mysqld keeps running, it just
loses connectivity — while the primary keeps majority and accepts writes. The node is
healed before the donor can purge its binary logs, so Group Replication catches it up
with an Incremental State Transfer rather than a full clone. Confirms mysqld stayed alive
throughout the partition, that no new clone was taken, that the node actually applied the
transactions it missed, and that data is consistent across all three nodes afterwards.
"""

import time

import pytest

from generic_helper import sql_str


def _query(gr_cluster, node, sql):
    """Run a read-only query on one node and return its raw tab-separated output."""
    return gr_cluster.docker.exec_mysql(
        node, sql, password=gr_cluster.root_password, timeout=30
    ).stdout.strip()


def _clone_snapshot(gr_cluster, node):
    """Return the node's clone history, used to detect whether a *new* clone was taken.

    Not simply asserted to be empty: create() adds every secondary with
    recoveryMethod:'clone', so each one already carries a completed clone row from
    cluster bootstrap. A rejoin via IST leaves that history untouched.
    """
    return _query(
        gr_cluster, node, "SELECT ID, STATE, BEGIN_TIME FROM performance_schema.clone_status;"
    )


def _gtid_subset(gr_cluster, node, gtid_set):
    """Return "1" if the node's gtid_executed is a superset of gtid_set, else "0"."""
    return _query(
        gr_cluster, node, f"SELECT GTID_SUBSET({sql_str(gtid_set)}, @@GLOBAL.gtid_executed);"
    )


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_secondary_isolation_ist_recovery(gr_cluster, sysbench):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Pick a secondary to partition and record its clone history before anything happens,
    # so the post-rejoin comparison can tell an IST from a fresh clone.
    target = gr_cluster.secondaries()[0]
    clone_before = _clone_snapshot(gr_cluster, target)

    # Sever the network only — mysqld on the target keeps running, unlike stop_node().
    gr_cluster.isolate_node(target)

    # The two survivors keep majority; confirm the group settles to exactly 2 ONLINE
    # members (failure detection and expulsion take a few seconds) without the target.
    states = gr_cluster.wait_online_count(2)
    online_hosts = [host for host, (state, _) in states.items() if state == "ONLINE"]
    assert target not in online_hosts, f"isolated node {target} still ONLINE: {states}"

    # The whole point of a network partition versus a stop: the process is untouched.
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    # From the minority side the target must see that it has lost the group. Polled,
    # because its own suspicion timer runs independently of the majority's expulsion.
    deadline = time.time() + 60
    target_view = gr_cluster.member_states(target)
    while {h for h, (state, _) in target_view.items() if state == "ONLINE"} - {target}:
        if time.time() >= deadline:
            break
        time.sleep(2)
        target_view = gr_cluster.member_states(target)
    still_online = {h for h, (state, _) in target_view.items() if state == "ONLINE"} - {target}
    assert not still_online, (
        f"isolated node {target} still sees group members as ONLINE: {target_view}"
    )

    # Accumulate writes the target will have to catch up on. The primary never lost
    # majority, so the read/write endpoint does not move during the partition window.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=30)

    # Still alive right before the heal — the partition never touched the process.
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    # The GTID set the target has to catch up on. Newlines are stripped because the
    # client prints one per UUID set and GTID_SUBSET() wants a single-line set.
    missed_gtids = _query(
        gr_cluster, gr_cluster.get_primary(), "SELECT @@GLOBAL.gtid_executed;"
    ).replace("\n", "")
    assert _gtid_subset(gr_cluster, target, missed_gtids) == "0", (
        f"{target} was not actually behind at the end of the partition window "
        "— the catch-up check below would prove nothing"
    )

    # Heal while the donor still has the binary logs covering the partition window, which
    # is what keeps the IST path available (binlog_expire_logs_seconds defaults to 30 days,
    # so a ~30s window can never outlive them). heal_node() reports whether GR rejoined the
    # member on its own or needed an explicit START GROUP_REPLICATION.
    auto_rejoined = gr_cluster.heal_node(target, timeout=120)
    gr_cluster.log(
        f"{target} rejoined {'automatically' if auto_rejoined else 'via explicit START GROUP_REPLICATION'}"
    )

    # IST, not SST: recovery must not have added a clone to the target's history.
    clone_after = _clone_snapshot(gr_cluster, target)
    assert clone_after == clone_before, (
        f"a new clone was taken on {target} (SST instead of IST):\n"
        f"before: {clone_before!r}\nafter: {clone_after!r}"
    )

    # ...and the node really did replay everything it missed while partitioned.
    # Deliberately not the spec's COUNT_TRANSACTIONS_REMOTE_APPLIED: that counter only
    # covers transactions received from the group once a member is already ONLINE, so it
    # reads 0 after a recovery-channel catch-up. The GTID set is the direct evidence.
    assert _gtid_subset(gr_cluster, target, missed_gtids) == "1", (
        f"{target} is still missing transactions from the partition window\n"
        f"expected superset of: {missed_gtids!r}\n"
        f"has: {_query(gr_cluster, target, 'SELECT @@GLOBAL.gtid_executed;')!r}"
    )

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the whole cluster again; data stays consistent across all three nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
