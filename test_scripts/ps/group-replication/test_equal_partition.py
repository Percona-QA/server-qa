"""Group Replication equal partition and split-brain prevention test.

This test is the only one needing a 4-node cluster. The cluster is split down the middle
into two halves of two, each half intact internally but holding only 2 of 4 — short of the
3-of-4 majority. Neither side may accept a write and neither may elect a primary of its own,
because either would be a split brain. Group Replication cannot recover from an even split
on its own: an operator has to choose a half and force its membership.

The split is done with reject routes inside the containers (sever_link), not by moving
containers between networks. A container that changes network changes IP, and XCOM does not
follow a peer to a new address, so moving a pair onto a network of their own makes them lose
*each other* — four one-node partitions, which is scenario 1's topology, not this one.
Blackholing the other half's addresses leaves every IP and process untouched, so each half
stays internally connected and only the link across the middle is gone.

Unlike test_majority_loss.py this leaves group_replication_unreachable_majority_timeout at
its default of 0, so the blocked members stay in the group rather than self-ejecting:
group_replication_force_members has to be run on a member that is still in the group. Writes
therefore hang rather than being rejected, which is why the write probes are bounded by a
timeout — a blocked write and a refused one are both "no write happened".
"""

import pytest

PROBE_TABLE = "sbtest.split_probe"
# Attempted from each half while neither has quorum. None of them may become visible while
# the split is in effect. The two that *block* (the kept half still holds the primary role,
# so GR parks the write awaiting consensus) do go on to commit once that half is unblocked —
# see the comment on the recovery assertions. The one aimed at the cut-off half is refused
# outright, because nothing there is writable, so it can never appear at all.
BLOCKED_NOTES = ("blocked-kept", "blocked-proxy")
REFUSED_NOTE = "refused-moved"
ALL_NOTES = (*BLOCKED_NOTES, REFUSED_NOTE)


def _probe_write(gr_cluster, node, note):
    """Attempt a write on one node, bounded so a blocked (rather than refused) write returns."""
    return gr_cluster.docker.exec_mysql(
        node,
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{note}');",
        password=gr_cluster.root_password,
        check=False,
        timeout=15,
    )


def _describe(result):
    """Say how a write probe failed — blocked until the timeout, or refused by the server."""
    return "blocked (timed out)" if result.returncode == 124 else result.stderr.strip()


def _count_notes(gr_cluster, node, notes):
    """Count probe rows with any of the given notes as visible on one node."""
    quoted = ", ".join(f"'{note}'" for note in notes)
    return gr_cluster.docker.exec_mysql(
        node,
        f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE note IN ({quoted});",
        password=gr_cluster.root_password,
    ).stdout.strip()


@pytest.mark.parametrize(
    "gr_cluster",
    [pytest.param(("router", 4), id="router"), pytest.param(("haproxy", 4), id="haproxy")],
    indirect=True,
)
def test_equal_partition(gr_cluster, sysbench):
    gr_cluster.verify()
    assert gr_cluster.num_nodes == 4

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

    # Split 2-2, keeping the current primary on the side we will later recover.
    primary = gr_cluster.get_primary()
    secondaries = gr_cluster.secondaries()
    kept = [primary, secondaries[0]]
    moved = secondaries[1:]
    assert len(moved) == 2, f"expected a 2-2 split, got kept={kept} moved={moved}"
    gr_cluster.sever_link(kept, moved)

    # The kept half is intact: it still sees its own pair, and the other two are gone.
    kept_view = gr_cluster.wait_members_unreachable(moved, node=primary)
    assert all(kept_view.get(n, ("", ""))[0] == "UNREACHABLE" for n in moved), (
        f"expected {moved} UNREACHABLE from {primary}, got {kept_view}"
    )
    assert kept_view.get(kept[1], ("", ""))[0] == "ONLINE", (
        f"{kept[1]} should still be ONLINE alongside {primary}: {kept_view}"
    )

    # ...and so is the moved half. This is what makes it an *equal* partition rather than
    # four one-node partitions, and it is the check that sever_link() worked at all: the
    # two cut-off nodes must still see each other.
    moved_view = gr_cluster.wait_members_unreachable(kept, node=moved[0])
    assert moved_view.get(moved[1], ("", ""))[0] == "ONLINE", (
        f"{moved[0]} and {moved[1]} lost contact — this is not a 2-2 split: {moved_view}"
    )
    assert all(moved_view.get(n, ("", ""))[0] == "UNREACHABLE" for n in kept), (
        f"expected {kept} UNREACHABLE from {moved[0]}, got {moved_view}"
    )

    # The whole point of a network partition versus a stop: every process is untouched.
    for node in gr_cluster.containers:
        assert gr_cluster.node_alive(node), f"mysqld on {node} died during the partition"

    # No split brain: the moved half must not promote a primary of its own. It holds 2 of 4,
    # so it has to stay read-only and leave the role where it was.
    moved_primaries = [
        h for h, (state, role) in moved_view.items() if role == "PRIMARY" and state == "ONLINE"
    ]
    assert not moved_primaries, (
        f"the minority half elected its own primary {moved_primaries} — split brain: {moved_view}"
    )
    for node in moved:
        assert gr_cluster.super_read_only(node) == "1", (
            f"{node} is writable while its half has no quorum"
        )

    # Neither half accepts a write. On the kept half the primary blocks (it still holds the
    # role but cannot reach a majority); on the moved half nothing is writable at all.
    for node, note in ((primary, BLOCKED_NOTES[0]), (moved[0], REFUSED_NOTE)):
        rejected = _probe_write(gr_cluster, node, note)
        assert not rejected.ok, f"{node} accepted a write with no quorum: {rejected.stdout!r}"
        gr_cluster.log(f"{node} did not accept the write: {_describe(rejected)}")

    # ...nor does the read/write endpoint.
    via_proxy = gr_cluster.exec_sql(
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{BLOCKED_NOTES[1]}');",
        check=False,
        timeout=30,
    )
    assert not via_proxy.ok, (
        f"{gr_cluster.proxy} accepted a write with no quorum: {via_proxy.stdout!r}"
    )
    gr_cluster.log(f"{gr_cluster.proxy} did not accept the write: {_describe(via_proxy)}")

    # And none of them became visible on either side of the split. This is the guarantee
    # that matters: while no half has a majority, no write is accepted anywhere.
    for node in (primary, moved[0]):
        seen = _count_notes(gr_cluster, node, ALL_NOTES)
        assert seen == "0", f"{node} accepted {seen} write(s) while its half had no quorum"

    # GR cannot resolve an even split by itself — an operator picks a half and forces its
    # membership. Both kept nodes are alive and can see each other, so unlike the
    # majority-loss case the forced list can name the whole surviving half rather than a
    # single node.
    survivors = gr_cluster.force_members(kept, node=primary)
    online = {h for h, (state, _) in survivors.items() if state == "ONLINE"}
    assert online == set(kept), f"forced membership did not settle on {kept}: {survivors}"

    # That half is a quorum of its own now, so writes work again.
    gr_cluster.docker.exec_mysql(
        gr_cluster.get_primary(),
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('recovered');",
        password=gr_cluster.root_password,
    )

    # Bring the other half back. Restoring connectivity is not enough on its own: both nodes
    # are still stuck in their stale blocked view, and the forced membership left them out
    # of the group entirely, so they have to restart Group Replication.
    gr_cluster.restore_link(kept, moved)
    for node in moved:
        gr_cluster.restart_group_replication(node)
    gr_cluster.wait_all_online(timeout=300, node=primary)

    # No refresh_proxy() here, unlike the tests that reconnect containers to the network:
    # severing a link leaves every address unchanged, so the proxy's backends are still valid.
    gr_cluster.wait_proxy_ready(timeout=300)

    # The write the cut-off half refused outright can never exist — nothing there was ever
    # writable. The two that merely *blocked* are a different matter: with
    # unreachable_majority_timeout at its default the primary parks such a write awaiting
    # consensus rather than failing it, so unblocking the kept half lets them commit, even
    # though the client that issued them is long gone. That is not a split brain — no
    # conflicting write was accepted on the other side — and the checksum comparison below
    # is what proves the four nodes agree on whatever did commit.
    for node in gr_cluster.active_nodes:
        leaked = _count_notes(gr_cluster, node, (REFUSED_NOTE,))
        assert leaked == "0", f"a refused write leaked onto {node} ({leaked} rows)"

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=180)

    # Load against the reunited cluster; data stays consistent across all four nodes.
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=180)
