"""Group Replication majority loss caused by node death.

Ticket cases 4 and 5. Two of three nodes are killed outright, leaving a single survivor
holding 1 of 3 — no majority, so it must refuse writes rather than accept anything it can
never replicate. Group Replication cannot recover from that on its own: an operator forces
the membership down to the survivor, which unblocks it and lets load run again. The dead
nodes are then restarted and rejoin.

Two victim combinations, because the survivor's starting role changes what has to happen:

- **secondaries** — both secondaries die and the primary survives. It keeps the role it
  already had; it just has to stop accepting writes until the membership is forced.
- **primary_and_secondary** — the primary and one secondary die. The surviving secondary is
  read-only to begin with, and forcing the membership has to **promote** it.

The sibling test_majority_loss.py reaches the same state by severing the network instead.
Killing is a different fault: the processes are gone rather than unreachable, so nothing on
the far side is left running to block, and recovery restarts containers instead of
reconnecting a link.
"""

import pytest

PROBE_TABLE = "sbtest.kill_probe"
NOTE_DIRECT = "no-quorum-direct"
NOTE_PROXY = "no-quorum-proxy"


def _count_note(gr_cluster, node, note):
    """How many probe rows with this note are visible on one node."""
    return gr_cluster.docker.exec_mysql(
        node,
        f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE note='{note}';",
        password=gr_cluster.root_password,
    ).stdout.strip()


def _describe(result):
    """Say how a write probe failed — blocked until the timeout, or refused by the server."""
    return "blocked (timed out)" if result.returncode == 124 else result.stderr.strip()


@pytest.mark.parametrize("gr_cluster", ["router", "haproxy"], indirect=True)
@pytest.mark.parametrize("victims", ["secondaries", "primary_and_secondary"])
def test_majority_loss_by_kill(gr_cluster, sysbench, victims):
    gr_cluster.verify()

    host, port = gr_cluster.rw_endpoint()
    sysbench.prepare(host=host, port=port)
    # Recreated, not CREATE IF NOT EXISTS: the two parametrised cases share this
    # module-scoped cluster, and the sysbench fixture's cleanup only drops sbtest1..4. The
    # first case's *blocked* write commits once force_members unblocks the primary — correct
    # behaviour, documented below — so without this the second case would start with that
    # row already present and read it as a write accepted without quorum.
    gr_cluster.exec_sql(
        f"DROP TABLE IF EXISTS {PROBE_TABLE};"
        f"CREATE TABLE {PROBE_TABLE} (id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(64));"
    )
    gr_cluster.verify_checksums("sbtest", timeout=120)

    # Choose sides before anything dies: once quorum is gone there is no elected primary
    # left to ask, so get_primary() would just spin until it times out.
    primary = gr_cluster.get_primary()
    secondaries = gr_cluster.secondaries()
    if victims == "secondaries":
        dead, survivor = secondaries, primary
    else:
        dead, survivor = [primary, secondaries[0]], secondaries[1]
    survivor_was_primary = survivor == primary

    for node in dead:
        gr_cluster.kill_node(node)

    # The survivor holds 1 of 3. It cannot expel the dead members without a majority, so
    # they sit in its view as UNREACHABLE and it blocks.
    view = gr_cluster.wait_members_unreachable(dead, node=survivor)
    assert all(view.get(n, ("", ""))[0] == "UNREACHABLE" for n in dead), (
        f"expected {dead} UNREACHABLE from {survivor}, got {view}"
    )

    # No write is accepted anywhere. How it fails depends on the survivor's role: a primary
    # parks the write awaiting consensus (so this times out), a secondary refuses it
    # outright as super_read_only.
    direct = gr_cluster.docker.exec_mysql(
        survivor,
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{NOTE_DIRECT}');",
        password=gr_cluster.root_password,
        check=False,
        timeout=15,
    )
    assert not direct.ok, f"{survivor} accepted a write with no quorum: {direct.stdout!r}"
    gr_cluster.log(f"{survivor} did not accept the write: {_describe(direct)}")

    via_proxy = gr_cluster.exec_sql(
        f"INSERT INTO {PROBE_TABLE} (note) VALUES ('{NOTE_PROXY}');", check=False, timeout=30
    )
    assert not via_proxy.ok, (
        f"{gr_cluster.proxy} accepted a write with no quorum: {via_proxy.stdout!r}"
    )
    gr_cluster.log(f"{gr_cluster.proxy} did not accept the write: {_describe(via_proxy)}")

    # ...and neither became visible while quorum was lost. That is the guarantee that holds
    # regardless of how the write failed.
    for note in (NOTE_DIRECT, NOTE_PROXY):
        seen = _count_note(gr_cluster, survivor, note)
        assert seen == "0", f"{survivor} accepted {note!r} ({seen} rows) with no quorum"

    # Unblock by forcing the membership down to the survivor — the documented recovery, and
    # the only one available: GR will not resolve this by itself.
    forced = gr_cluster.force_members([survivor], node=survivor)
    online = {h for h, (state, _) in forced.items() if state == "ONLINE"}
    assert online == {survivor}, f"forced membership did not settle on {survivor}: {forced}"

    # A single-member group elects its own primary, so the survivor is writable — for the
    # primary_and_secondary case that means a read-only secondary has just been promoted.
    assert gr_cluster.get_primary() == survivor, (
        f"{survivor} was not promoted after the membership was forced"
    )
    if not survivor_was_primary:
        gr_cluster.log(f"{survivor} was promoted from SECONDARY to PRIMARY")

    # The group executes load again.
    gr_cluster.wait_proxy_ready(timeout=300)
    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)

    # Restart the dead nodes; they rejoin the reformed group.
    gr_cluster.rejoin_nodes(dead, timeout=300)

    # A stopped container can come back on a new IP, and HAProxy resolves its backends once
    # at start time, so rebuild it before relying on the endpoint again.
    gr_cluster.refresh_proxy()
    gr_cluster.wait_proxy_ready(timeout=300)

    # The write the survivor refused outright can never exist. The one it *blocked* is a
    # different matter — a parked write commits once the survivor is unblocked, even though
    # its client is long gone — so that one is only asserted to have been invisible during
    # the outage, above, with verify_checksums() covering agreement on whatever did commit.
    if not survivor_was_primary:
        for node in gr_cluster.active_nodes:
            leaked = _count_note(gr_cluster, node, NOTE_DIRECT)
            assert leaked == "0", f"a refused write leaked onto {node} ({leaked} rows)"

    gr_cluster.verify()
    gr_cluster.verify_checksums("sbtest", timeout=180)

    host, port = gr_cluster.rw_endpoint()
    sysbench.run(host=host, port=port, time=20)
    gr_cluster.verify_checksums("sbtest", timeout=180)
