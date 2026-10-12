"""Group Replication isolation timing against the two membership timeouts.

Scenarios 6-10 of PS-11378. One node is cut off with `docker network disconnect` and
reconnected at a chosen point relative to two timeouts that act on *opposite* sides of the
partition:

- `group_replication_member_expel_timeout` (E) — how long the **majority** tolerates a
  suspected member before expelling it. Defaults to 5s.
- `group_replication_unreachable_majority_timeout` (U) — how long the **isolated** member
  waits before giving up and leaving the group itself. Defaults to 0, meaning never.

Reconnect inside both and nothing should happen to the membership at all: the member goes
UNREACHABLE and comes back without ever leaving the view. Overshoot either one and the
member is out, and has to come back through group_replication_autorejoin_tries on its own.

Each case sets both timeouts explicitly rather than racing the defaults, so the outage sits
unambiguously inside the intended window. The partition is made with sever_link()'s reject
routes rather than by detaching the container: reconnecting to a network reassigns the
address, and XCOM cannot resume a session with a peer that moved, so the member would be
expelled and have to rejoin no matter how briefly it was away — which is precisely what
scenarios 6 and 7 are supposed to rule out. Blackholing the peers leaves every address
untouched, so a member reconnected inside both windows genuinely resumes, and the cases
behave the same on any container runtime. Runs behind HAProxy only — nothing here asserts
routing, and the one proxy-relevant behaviour (the write endpoint following a new primary)
is covered under both proxies by test_primary_isolation_failover.py, which is the same
family's fourth quadrant: a primary reconnected after *both* timeouts.
"""

import time

import pytest

PROBE_TABLE = "gr_test.timeouts"


def _seed_data(gr_cluster):
    """Create and populate a small probe table; these cases are about membership, not volume."""
    gr_cluster.exec_sql("CREATE DATABASE IF NOT EXISTS gr_test;")
    gr_cluster.exec_sql(f"DROP TABLE IF EXISTS {PROBE_TABLE};")
    gr_cluster.exec_sql(
        f"CREATE TABLE {PROBE_TABLE} (id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(32));"
    )
    gr_cluster.exec_sql(f"INSERT INTO {PROBE_TABLE} (note) VALUES ('a'),('b'),('c');")
    gr_cluster.verify_checksums("gr_test", timeout=120)


def _wait_left_group(gr_cluster, node, timeout=120):
    """Poll until the node reports itself out of the group; return the last state seen."""
    deadline = time.time() + timeout
    while True:
        state = gr_cluster.local_member_state(node)
        if state in ("OFFLINE", "ERROR") or time.time() >= deadline:
            return state
        time.sleep(2)


def _pick(gr_cluster, role):
    """Return (target, observer, others): the node to cut off, a majority witness, and the rest."""
    primary = gr_cluster.get_primary()
    secondaries = gr_cluster.secondaries()
    target, observer = (primary, secondaries[0]) if role == "primary" else (secondaries[0], primary)
    others = [node for node in gr_cluster.containers if node != target]
    return target, observer, others


@pytest.mark.parametrize("gr_cluster", ["haproxy"], indirect=True)
@pytest.mark.parametrize("role", ["secondary", "primary"])
def test_member_not_expelled(gr_cluster, role):
    """Scenarios 6 and 7 — reconnect inside both timeouts; the member is never expelled."""
    gr_cluster.verify()
    _seed_data(gr_cluster)

    # Both windows far longer than the outage, so neither side acts on the partition.
    gr_cluster.set_global("group_replication_member_expel_timeout", 120)
    gr_cluster.set_global("group_replication_unreachable_majority_timeout", 120)

    target, observer, others = _pick(gr_cluster, role)
    primary_before = gr_cluster.get_primary()
    view_before = gr_cluster.view_id(observer)
    clone_before = gr_cluster.clone_status(target)
    assert view_before, f"could not read a VIEW_ID from {observer}"

    gr_cluster.sever_link(others, [target])

    # The fault has to actually register, or this case proves nothing: a partition too short
    # to notice would trivially leave the view unchanged.
    view = gr_cluster.wait_members_unreachable([target], node=observer)
    assert view.get(target, ("", ""))[0] == "UNREACHABLE", (
        f"{observer} never saw {target} go UNREACHABLE: {view}"
    )
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    # Still inside both windows when connectivity comes back.
    time.sleep(20)
    gr_cluster.restore_link(others, [target])
    gr_cluster.wait_all_online(timeout=180, node=observer)

    # The decisive check: a membership change bumps the view, so an unchanged VIEW_ID means
    # the member was never removed — it only ever went UNREACHABLE inside the same view.
    view_after = gr_cluster.view_id(observer)
    assert view_after == view_before, (
        f"{target} was expelled and the group reformed: view {view_before} -> {view_after}"
    )
    assert gr_cluster.clone_status(target) == clone_before, (
        f"{target} needed recovery despite never leaving the group"
    )
    if role == "primary":
        assert gr_cluster.get_primary() == primary_before, (
            f"primary moved off {primary_before} without a view change"
        )

    gr_cluster.verify()
    gr_cluster.verify_checksums("gr_test", timeout=120)


@pytest.mark.parametrize("gr_cluster", ["haproxy"], indirect=True)
@pytest.mark.parametrize(
    "role,expel_timeout,unreachable_timeout",
    [
        pytest.param("secondary", 300, 20, id="secondary-after-unreachable"),
        pytest.param("primary", 300, 20, id="primary-after-unreachable"),
        pytest.param("primary", 20, 600, id="primary-after-expel"),
    ],
)
def test_member_expelled_and_autorejoins(gr_cluster, role, expel_timeout, unreachable_timeout):
    """Scenarios 8, 9 and 10 — overshoot one timeout; the member leaves and auto-rejoins.

    Which timeout fires first decides far more than which code path removes the member, and
    the two role="primary" cases are where it shows:

    - E first (primary-after-expel): the *group* removes the member, so the view changes and
      a replacement primary is elected straight away, while the member itself has not given
      up and sits blocked in its stale view.
    - U first (primary-after-unreachable): the member leaves of its own accord, but it is
      partitioned and cannot announce that, so the group keeps it in the view as an
      unreachable PRIMARY and elects nobody. The election only happens when it comes back
      and rejoins as a secondary.
    """
    gr_cluster.verify()
    _seed_data(gr_cluster)

    gr_cluster.set_global("group_replication_member_expel_timeout", expel_timeout)
    gr_cluster.set_global("group_replication_unreachable_majority_timeout", unreachable_timeout)

    target, observer, others = _pick(gr_cluster, role)
    removed_by_group = expel_timeout < unreachable_timeout

    gr_cluster.sever_link(others, [target])
    view = gr_cluster.wait_members_unreachable([target], node=observer)
    assert view.get(target, ("", ""))[0] == "UNREACHABLE", (
        f"{observer} never saw {target} go UNREACHABLE: {view}"
    )
    assert gr_cluster.node_alive(target), f"mysqld on {target} died during the partition"

    if removed_by_group:
        # E fires first: the survivors drop it from the view entirely...
        settled = gr_cluster.wait_membership(others, node=observer, timeout=180)
        gr_cluster.log(f"group removed {target} after expel_timeout: {settled}")
        # ...and with the primary gone that is a membership change, so a replacement is
        # elected while the old one is still cut off.
        if role == "primary":
            assert gr_cluster.get_primary() != target, (
                f"no replacement elected after the group expelled {target}"
            )
        # The member itself has not given up — U is far away — so it is blocked, not
        # read-only, and does not know it has been removed.
        assert gr_cluster.super_read_only(target) == "0", (
            f"{target} went read-only before unreachable_majority_timeout "
            f"({unreachable_timeout}s) could fire"
        )
    else:
        # U fires first: the member leaves on its own, and exit_state_action applies.
        state = _wait_left_group(gr_cluster, target)
        assert state in ("OFFLINE", "ERROR"), (
            f"{target} passed unreachable_majority_timeout but still reports {state!r}"
        )
        # It could not tell anyone, so the group still lists it — and for a primary that
        # means no election yet, because nothing changed the membership.
        held = gr_cluster.member_states(observer)
        assert set(held) == set(gr_cluster.containers), (
            f"group dropped {target} despite expel_timeout={expel_timeout}s: {held}"
        )
        if role == "primary":
            online_primaries = [
                host for host, (st, r) in held.items() if st == "ONLINE" and r == "PRIMARY"
            ]
            assert not online_primaries, (
                f"a primary was elected while {target} was still in the view: {held}"
            )

    # Reconnect and let group_replication_autorejoin_tries do the work. heal_node() reports
    # False if it had to fall back to an explicit START GROUP_REPLICATION, which is exactly
    # the failure these scenarios are about.
    rejoin_started = time.monotonic()
    gr_cluster.restore_link(others, [target])
    auto_rejoined = gr_cluster.heal_node(target, timeout=600, rejoin_grace=360)
    gr_cluster.log(f"{target} rejoined after {time.monotonic() - rejoin_started:.0f}s")
    tries = gr_cluster.docker.exec_mysql(
        target,
        "SELECT @@GLOBAL.group_replication_autorejoin_tries;",
        password=gr_cluster.root_password,
    ).stdout.strip()
    assert auto_rejoined, (
        f"{target} needed an explicit START GROUP_REPLICATION; "
        f"group_replication_autorejoin_tries ({tries}) should have brought it back"
    )

    # Let the view settle before judging the rejoin. heal_node() returns as soon as every
    # member reads ONLINE, which can catch the auto-rejoin still finishing — the member has
    # been declared online but distributed recovery is not done with it, and the membership
    # moves again a moment later.
    gr_cluster.wait_view_stable(observer)

    final_primary = gr_cluster.get_primary()
    members = gr_cluster.member_states(final_primary)
    assert all(state == "ONLINE" for state, _ in members.values()), f"not all ONLINE: {members}"
    assert members.get(target) == ("ONLINE", "SECONDARY"), (
        f"{target} did not rejoin as a secondary: {members}"
    )
    if role == "primary":
        # Either way the role has moved on: expelled during the outage, or on the rejoin.
        assert final_primary != target, f"{target} is primary again after rejoining"

    # exit_state_action's offline mode has to be cleared, or the node is ONLINE to the group
    # but still refuses ordinary clients.
    offline_mode = gr_cluster.docker.exec_mysql(
        target, "SELECT @@GLOBAL.offline_mode;", password=gr_cluster.root_password
    ).stdout.strip()
    assert offline_mode == "0", f"{target} is still in offline mode ({offline_mode!r})"

    # The role moved, so the write endpoint has to follow before verify() checks it. No
    # refresh_proxy() is needed: severing a link leaves every address intact, so HAProxy's
    # backends are still valid and only the write pin has to be moved.
    gr_cluster.wait_proxy_ready(timeout=300)

    gr_cluster.verify()
    gr_cluster.verify_checksums("gr_test", timeout=120)
