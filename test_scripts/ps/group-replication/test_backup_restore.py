"""Group Replication XtraBackup full + incremental backup/restore test.

Takes a full backup of a cluster secondary, runs more load, takes an incremental
backup, then (after yet more load) prepares and restores the full+incremental chain
into a fresh standalone node. Confirms the restored data matches the cluster's state as
of the incremental backup (a point-in-time check), and that the live cluster is
unaffected and group replication still healthy. Runs behind HAProxy only — the proxy is
irrelevant to backup/restore.
"""

import pytest


@pytest.mark.parametrize("gr_cluster", ["haproxy"], indirect=True)
def test_full_and_incremental_backup_restore(gr_cluster, sysbench, xtrabackup):
    # Healthy 3-node cluster to start from.
    gr_cluster.verify()

    # Initial data load via sysbench (4 tables x 10000 rows) through the read/write endpoint.
    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Back up a secondary (never the primary); XtraBackup reads its data volume directly.
    # The data volume follows the "<container>-data" convention, so derive it from the node
    # name rather than reaching into GroupReplication internals.
    secondary = gr_cluster.secondaries()[0]
    src_vol = f"{secondary}-data"

    # Full backup of the current data (set A).
    xtrabackup.helper.full_backup(secondary, src_vol)

    # More load (set B), and make sure the secondary has fully applied it.
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Incremental backup capturing set B, and snapshot that point-in-time state.
    xtrabackup.helper.incremental_backup(secondary, src_vol)
    snapshot = gr_cluster.table_checksums(secondary, "sbtest")

    # Yet more load (set C) — this is intentionally NOT in the backup chain.
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Prepare the full+incremental chain and restore it into a fresh standalone node.
    xtrabackup.helper.prepare()
    xtrabackup.helper.copy_back(xtrabackup.restore_volume)
    node = gr_cluster.start_standalone_node(
        xtrabackup.restore_container, xtrabackup.restore_volume
    )

    # The restored data equals the state at incremental-backup time (set B), not the
    # later set C — proving a real point-in-time restore, not just any consistent state.
    restored = gr_cluster.table_checksums(node, "sbtest")
    assert restored == snapshot, (
        f"restored data mismatch:\n  restored={restored}\n  snapshot={snapshot}"
    )

    # The live cluster was never disrupted and group replication is still healthy.
    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=120)
