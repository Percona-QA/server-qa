"""Group Replication minority node isolation with SST (clone) recovery test.

Scenario 3 of gr_partition_scenarios.md, the mirror image of the IST test. A single
secondary of a 3-node cluster is network-partitioned with `docker network disconnect` —
its mysqld keeps running, it just loses connectivity — while the primary keeps majority
and accepts writes. Before healing, the binary logs are purged on every surviving node so
no donor can serve an Incremental State Transfer, forcing Group Replication to fall back
to a full clone. Confirms mysqld stayed alive throughout the partition, that a new clone
really did run and completed without error, and that data is consistent across all three
nodes afterwards.

Percona Server 8.4 uses the clone plugin for distributed recovery, so this exercises
clone-based SST rather than the XtraBackup SST path.
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_secondary_isolation_sst_recovery(gr_cluster, sysbench):
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Pick a secondary to partition and record its clone history before anything happens.
    # create() adds every secondary with recoveryMethod:'clone', so this is already
    # populated — "clone_status has rows" would pass even for an IST, which is why the
    # assertion below compares against this snapshot rather than just checking for a row.
    target = gr_cluster.secondaries()[0]
    clone_before = gr_cluster.clone_status(target)

    # Sever the network only — mysqld on the target keeps running, unlike stop_node().
    gr_cluster.isolate_node(target)

    # The two survivors keep majority; confirm the group settles to exactly 2 ONLINE
    # members (failure detection and expulsion take a few seconds) without the target.
    states = gr_cluster.wait_online_count(2)
    online_hosts = [host for host, (state, _) in states.items() if state == "ONLINE"]
    assert target not in online_hosts, f"isolated node {target} still ONLINE: {states}"

    # The whole point of a network partition versus a stop: the process is untouched.
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    # From the minority side the target must see that it has lost the group.
    target_view = gr_cluster.wait_node_isolated(target)
    still_online = {h for h, (state, _) in target_view.items() if state == "ONLINE"} - {target}
    assert not still_online, (
        f"isolated node {target} still sees group members as ONLINE: {target_view}"
    )

    # Accumulate the writes the target misses. The primary never lost majority, so the
    # read/write endpoint does not move during the partition window.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=30)

    # Still alive right before the heal — the partition never touched the process.
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    # The GTID set the target has to recover. Asserting it is genuinely behind first keeps
    # the post-recovery catch-up check from passing vacuously.
    missed_gtids = gr_cluster.gtid_executed(gr_cluster.get_primary())
    assert not gr_cluster.gtid_subset(target, missed_gtids), (
        f"{target} was not actually behind at the end of the partition window "
        "— the recovery checks below would prove nothing"
    )

    # Close off the IST path. This has to happen on *every* survivor, not just the primary
    # as the scenario doc suggests: GR picks its recovery donor from any ONLINE member, and
    # the other secondary carries full binary logs too (--log-replica-updates=ON), so
    # leaving it untouched would let it serve an IST and silently turn this into the IST
    # test. A non-empty gtid_purged is the proof that something was actually purged.
    purged = gr_cluster.purge_binary_logs()
    empty = [node for node, gtids in purged.items() if not gtids]
    assert not empty, f"binary logs were not purged on {empty} (gtid_purged still empty): {purged}"

    # With the missing transactions gone from every donor, GR has no choice but to clone.
    # A full clone copies the dataset and restarts mysqld on the recipient, so it gets a
    # longer budget than the IST path.
    auto_rejoined = gr_cluster.heal_node(target, timeout=240)
    gr_cluster.log(
        f"{target} rejoined {'automatically' if auto_rejoined else 'via explicit START GROUP_REPLICATION'}"
    )

    # SST, not IST: recovery must have run a new clone, and it must have finished cleanly.
    clone_after = gr_cluster.clone_status(target)
    assert clone_after != clone_before, (
        f"no new clone was taken on {target} (IST instead of SST):\n"
        f"before: {clone_before!r}\nafter: {clone_after!r}"
    )
    assert clone_after.get("STATE") == "Completed", (
        f"clone on {target} did not complete: {clone_after}"
    )
    assert clone_after.get("ERROR_NO") == "0", f"clone on {target} reported an error: {clone_after}"

    # ...and the cloned node really did come back with everything it missed.
    assert gr_cluster.gtid_subset(target, missed_gtids), (
        f"{target} is still missing transactions from the partition window\n"
        f"expected superset of: {missed_gtids!r}\n"
        f"has: {gr_cluster.gtid_executed(target)!r}"
    )

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Load against the whole cluster again; data stays consistent across all three nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
