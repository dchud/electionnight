"""The dashboard's automatic slowdown when queries get slow."""

from types import SimpleNamespace

from monitor.web.hub import Hub


def hub_at(settings, p95):
    state = {"p95": p95}
    model = SimpleNamespace(
        recent_p95_ms=lambda: state["p95"],
        snapshot=SimpleNamespace(
            snapshot_id=1, committed_at="now", changed=frozenset()
        ),
    )
    return Hub(model, settings), state


def test_new_data_goes_out_at_once_when_queries_are_fast(settings):
    hub, _ = hub_at(settings, 10)
    hub._update_slow()
    hub.pending = True
    assert hub._snapshot_due(1.0) is True


def test_slow_queries_stretch_the_gap_between_refreshes(settings):
    gap = settings.web.slow_throttle_seconds
    hub, _ = hub_at(settings, settings.web.slow_query_ms + 1)
    hub._update_slow()
    assert hub.slow is True
    hub.pending = True
    hub.last_snapshot_sent = 100.0
    assert hub._snapshot_due(100.0 + gap - 1) is False
    assert hub._snapshot_due(100.0 + gap) is True


def test_nothing_goes_out_without_new_data(settings):
    hub, _ = hub_at(settings, 10)
    assert hub._snapshot_due(1000.0) is False


def test_recovery_needs_p95_below_half_the_limit(settings):
    limit = settings.web.slow_query_ms
    hub, state = hub_at(settings, limit + 1)
    hub._update_slow()
    state["p95"] = limit * 0.75
    hub._update_slow()
    assert hub.slow is True
    state["p95"] = limit * 0.25
    hub._update_slow()
    assert hub.slow is False


def test_a_published_snapshot_clears_pending(settings):
    hub, _ = hub_at(settings, 10)
    queue = hub.subscribe()
    hub.pending = True
    hub._publish_snapshot(5.0)
    assert hub.pending is False
    assert hub.last_snapshot_sent == 5.0
    assert queue.get_nowait()["type"] == "snapshot"
