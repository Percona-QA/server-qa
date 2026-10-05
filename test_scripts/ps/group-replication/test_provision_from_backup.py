"""Group Replication node provisioning from an XtraBackup restore.

Scenario 1 of PS-11378. A full XtraBackup backup is taken from an online secondary, load
runs on so the backup falls behind, and the backup is then restored into a brand new node
which joins the cluster by replaying what it missed — not by cloning. That is the point of
provisioning this way: the bulk of the data arrives from the backup, and Group Replication
only has to carry the delta.

Where test_backup_restore.py stops at a standalone node for inspection, this one takes the
restored datadir the rest of the way into the group. A physical backup is a byte copy of
the donor, so the node first has to stop being the donor: start_restored_node() drops the
copied auto.cnf (the donor's server_uuid, which no two members may share) and
mysqld-auto.cnf (the donor's persisted group_replication_local_address and seeds).
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
def test_provision_from_backup(gr_cluster, sysbench, xtrabackup):
    gr_cluster.verify()
    assert gr_cluster.num_nodes == 3

    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Back up a secondary, never the primary: XtraBackup reads the data volume directly, and
    # the volume follows the "<container>-data" convention.
    secondary = gr_cluster.secondaries()[0]
    xtrabackup.helper.full_backup(secondary, f"{secondary}-data")
    snapshot = gr_cluster.table_checksums(secondary, "sbtest")

    # Load that the backup does not contain, so the provisioned node has real catching up to
    # do rather than arriving complete.
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)
    missed_gtids = gr_cluster.gtid_executed(gr_cluster.get_primary())

    # Restore the backup into the volume the next node will use, then bring that node up.
    xtrabackup.helper.prepare(incremental=False)
    xtrabackup.helper.copy_back(gr_cluster.next_node_volume())
    new_node = gr_cluster.start_restored_node()

    # It really did come up on the backup's data. Without this the test would prove little:
    # a cluster this short-lived still has every transaction in its binary logs, so an empty
    # node could join incrementally too and the restore would be doing no work.
    restored = gr_cluster.table_checksums(new_node, "sbtest")
    assert restored == snapshot, (
        f"{new_node} did not come up on the restored data:\n"
        f"  restored={restored}\n  backup={snapshot}"
    )
    assert not gr_cluster.gtid_subset(new_node, missed_gtids), (
        f"{new_node} is already up to date before joining, so nothing would be replayed"
    )
    # Clone history as the restore left it. The backup donor is a secondary that create()
    # added with recoveryMethod:'clone', so its datadir carries a completed clone row that the
    # byte copy can bring along — start_restored_node() scrubs auto.cnf and mysqld-auto.cnf,
    # nothing else. "No row" is therefore not a safe expectation, the same trap the IST/SST
    # tests document on clone_status(); compare before/after instead.
    clone_before = gr_cluster.clone_status(new_node)
    gr_cluster.log(f"{new_node} restored at {gr_cluster.gtid_executed(new_node)!r}, "
                   f"clone history {clone_before or 'empty'}")

    # Join by replaying the delta. recoveryMethod:'incremental' makes AdminAPI fail outright
    # rather than silently falling back to clone if the donors cannot serve it.
    gr_cluster.join_node(new_node)

    # Four members, the new one a secondary, still exactly one primary.
    members = gr_cluster.member_states(gr_cluster.get_primary())
    assert sorted(members) == sorted(gr_cluster.containers), f"membership incomplete: {members}"
    assert all(state == "ONLINE" for state, _ in members.values()), f"not all ONLINE: {members}"
    assert members.get(new_node) == ("ONLINE", "SECONDARY"), (
        f"{new_node} did not join as a secondary: {members}"
    )
    assert [role for _, role in members.values()].count("PRIMARY") == 1, (
        f"expected exactly one PRIMARY across the four nodes, got {members}"
    )

    # Provisioned from the backup, not reseeded: a join that cloned would have replaced the
    # row captured above.
    clone_after = gr_cluster.clone_status(new_node)
    assert clone_after == clone_before, (
        f"{new_node} was cloned instead of using the restored data:\n"
        f"before: {clone_before!r}\nafter: {clone_after!r}"
    )

    # ...and it replayed what the backup was missing.
    assert gr_cluster.gtid_subset(new_node, missed_gtids), (
        f"{new_node} never caught up on the load taken after the backup\n"
        f"expected superset of: {missed_gtids!r}\n"
        f"has: {gr_cluster.gtid_executed(new_node)!r}"
    )

    # The new member has to be reachable through the proxy too, not just in the group.
    gr_cluster.refresh_proxy()
    gr_cluster.wait_proxy_ready(timeout=300)
    serving = gr_cluster.wait_node_serving_reads(new_node)
    assert new_node in serving, (
        f"{new_node} is not serving reads after joining — the {gr_cluster.proxy} read "
        f"endpoint only ever answered from {sorted(serving)}"
    )

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=180)

    # Load against the grown cluster; data stays consistent across all four nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=180)
