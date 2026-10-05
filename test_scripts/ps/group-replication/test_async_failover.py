"""Asynchronous replication between two GR clusters, across a primary failover on either side.

A channel from a source cluster to a replica cluster, running with
`SOURCE_CONNECTION_AUTO_FAILOVER = 1` and a *managed* source list naming the source's group.
That combination is what lets the channel follow the source group's primary by itself rather
than being pinned to one host, and the two cases here fail over each side in turn:

- **source** — kill the source cluster's primary. The channel should reconnect to whichever
  member is elected in its place, with no intervention, and keep delivering.
- **replica** — kill the replica cluster's primary. The channel should come back on the
  newly elected replica primary and catch up on whatever it missed.

Setting this up has four non-obvious requirements, all of them discovered the hard way and
documented on the helpers that encode them: the two clusters need distinct `server_id`
ranges, the channel needs `GET_SOURCE_PUBLIC_KEY` because the server's default
`caching_sha2_password` will not send a password over a non-TLS connection, the channel has
to exist on every replica member *before* the primary is configured, or the members without
it leave the group (MY-013786), and every member needs a filter excluding the InnoDB Cluster
metadata or the source's copy silently overwrites the replica's (see `set_channel_filter`).
"""

import time

import pytest

CHANNEL = "async"
PROBE = "async_test.t"


@pytest.fixture(scope="module")
def async_channel(gr_cluster_pair):
    """Wire the source cluster to the replica cluster and return the pair, channel running."""
    source, replica = gr_cluster_pair.source, gr_cluster_pair.replica
    source.verify()
    replica.verify()

    source.create_replication_user()
    source.exec_sql("CREATE DATABASE IF NOT EXISTS async_test;")
    source.exec_sql(
        f"CREATE TABLE IF NOT EXISTS {PROBE} (id INT AUTO_INCREMENT PRIMARY KEY, n VARCHAR(32));"
    )

    replica.configure_async_channel(CHANNEL, source_host=source.get_primary())
    replica.add_managed_source(CHANNEL, source.group_name, source.get_primary())
    replica.start_async_channel(CHANNEL)
    state, host = replica.wait_channel_connected(CHANNEL)
    assert state == "ON", f"channel never connected: state={state!r} host={host!r}"
    return gr_cluster_pair


def _write(source, note):
    """Write straight to the source primary, not through its proxy.

    The proxy plays no part in this test and, right after a source failover, its write
    backend is still pinned to the node we just killed.
    """
    source.docker.exec_mysql(
        source.get_primary(), f"INSERT INTO {PROBE} (n) VALUES ('{note}');",
        password=source.root_password, timeout=60,
    )


def _count_rows(replica, node=None):
    """Rows in the probe table as one replica member sees them, or '' if unreadable."""
    target = node or replica.get_primary()
    return replica.docker.exec_mysql(
        target, f"SELECT COUNT(*) FROM {PROBE};", password=replica.root_password,
        check=False, timeout=30,
    ).stdout.strip()


def _metadata_owner(cluster, node=None):
    """The cluster_name this cluster's own InnoDB Cluster metadata claims, or '' if unreadable."""
    target = node or cluster.get_primary()
    return cluster.docker.exec_mysql(
        target, "SELECT cluster_name FROM mysql_innodb_cluster_metadata.clusters;",
        password=cluster.root_password, check=False, timeout=30,
    ).stdout.strip()


def _wait_rows(replica, expected, node=None, timeout=120):
    """Poll the replica cluster until the probe table has `expected` rows; return what it had."""
    deadline = time.time() + timeout
    while True:
        got = _count_rows(replica, node=node)
        if got == str(expected) or time.time() >= deadline:
            return got
        time.sleep(2)


@pytest.mark.parametrize("failover", ["source", "replica"])
def test_async_channel_survives_failover(async_channel, failover):
    source, replica = async_channel.source, async_channel.replica

    # The managed entry expands by itself to every member of the source group — that list is
    # what the channel falls back to, so without it a source failover would strand it.
    sources = replica.async_failover_sources(CHANNEL)
    assert sorted(sources) == sorted(source.active_nodes), (
        f"failover source list does not cover the source group's live members: {sources}"
    )
    assert len(sources) > 1, "a single-entry source list leaves the channel nowhere to fail over to"

    before = int(_count_rows(replica) or 0)
    _write(source, f"before-{failover}")
    got = _wait_rows(replica, before + 1)
    assert got == str(before + 1), f"baseline row never replicated: rows={got}"

    # Whatever happens, put the killed node back: the two cases share one module-scoped pair,
    # so leaving a cluster short would fail the other case for an unrelated reason.
    killed: list[tuple] = []
    try:
        _run_failover(source, replica, failover, before, killed)
    finally:
        for cluster, node in killed:
            if node not in cluster.active_nodes:
                cluster.rejoin_nodes([node], timeout=300)
                # A restarted member comes back with no replication filter: CHANGE REPLICATION
                # FILTER does not persist. Without this the returning replica member would
                # apply the source's InnoDB Cluster metadata the moment it ran the channel.
                if cluster is replica:
                    cluster.set_channel_filter(CHANNEL, node=node)

    # The filter is doing its job: this cluster's metadata still describes *this* cluster.
    # Unfiltered, the source's rows overwrite these by primary key without any error — the
    # applier stays ON and every assertion above still passes, so nothing else here catches it.
    assert _metadata_owner(replica) == replica.cluster_name, (
        f"replica metadata was overwritten by the source cluster: "
        f"cluster_name={_metadata_owner(replica)!r}, expected {replica.cluster_name!r}"
    )


def _run_failover(source, replica, failover, before, killed):
    if failover == "source":
        old = source.get_primary()
        source.kill_node(old)
        killed.append((source, old))
        source.wait_online_count(2, node=source.active_nodes[0])
        new = source.get_primary()
        assert new != old, f"source primary did not move off {old}"

        # The whole point of the managed list: the channel re-points itself.
        state, host = replica.wait_channel_connected(CHANNEL, source=new, timeout=240)
        assert (state, host) == ("ON", new), (
            f"channel did not follow the source failover: state={state!r} host={host!r}, "
            f"expected ON against {new}"
        )
        replica_node = None
    else:
        old = replica.get_primary()
        replica.kill_node(old)
        killed.append((replica, old))
        replica.wait_online_count(2, node=replica.active_nodes[0])
        new = replica.get_primary()
        assert new != old, f"replica primary did not move off {old}"

        # The channel has to come back up on whichever member now leads the replica group.
        state, host = replica.wait_channel_connected(CHANNEL, node=new, timeout=240)
        assert state == "ON", (
            f"channel did not resume on the new replica primary {new}: "
            f"state={state!r} host={host!r}"
        )
        replica_node = new

    # ...and it is still delivering afterwards.
    _write(source, f"after-{failover}")
    got = _wait_rows(replica, before + 2, node=replica_node, timeout=180)
    assert got == str(before + 2), (
        f"rows written after the {failover} failover never arrived: rows={got}"
    )

