import math

import pandas as pd
import pytest

from transit_ripple.config import Settings
from transit_ripple.impact import (
    SignalInput,
    impact_score,
    impact_series,
    sub_score,
    surface_recovery,
)

# Weights traffic 0.4, camera 0.3, bikes 0.3 (bus 0.3 but absent unless given);
# full changes: traffic -40%, bus -40%, camera +100%, bikes -30%.
S = Settings(
    impact_weight_traffic=0.4,
    impact_weight_bus=0.3,
    impact_weight_camera=0.3,
    impact_weight_bikes=0.3,
)


def ok(observed, baseline):
    return SignalInput(observed, baseline, "ok")


def test_sub_scores_are_linear_clipped_and_directional():
    assert sub_score("traffic", -20.0, S) == 50.0  # half of a 40% drop
    assert sub_score("traffic", -80.0, S) == 100.0  # clipped
    assert sub_score("traffic", 15.0, S) == 0.0  # faster traffic is no impact
    assert sub_score("camera", 50.0, S) == 50.0
    assert sub_score("camera", -30.0, S) == 0.0
    assert sub_score("bikes", -15.0, S) == 50.0
    with pytest.raises(ValueError):
        sub_score("buses", 1.0, S)


def test_all_signals_weighted_mean():
    res = impact_score(
        {
            "traffic": ok(15.0, 20.0),  # -25% -> 62.5
            "camera": ok(30.0, 20.0),  # +50% -> 50
            "bikes": ok(85.0, 100.0),  # -15% -> 50
        },
        S,
    )
    assert res.used == ["traffic", "camera", "bikes"]
    assert res.score == pytest.approx(0.4 * 62.5 + 0.3 * 50 + 0.3 * 50, abs=0.05)
    assert sum(r.effective_weight or 0 for r in res.signals.values()) == pytest.approx(1.0)


def test_missing_signal_renormalizes_over_the_rest():
    res = impact_score({"traffic": ok(15.0, 20.0), "bikes": ok(85.0, 100.0)}, S)
    assert res.used == ["traffic", "bikes"]
    # 0.4 and 0.3 renormalized to 4/7 and 3/7: a missing camera is not "no impact".
    assert res.score == pytest.approx((0.4 * 62.5 + 0.3 * 50) / 0.7, abs=0.05)
    assert res.signals["camera"].status == "no_data"
    assert res.signals["camera"].effective_weight is None
    assert res.signals["traffic"].effective_weight == pytest.approx(4 / 7, abs=1e-4)


def test_low_confidence_and_missing_baselines_are_excluded_but_reported():
    res = impact_score(
        {
            "traffic": SignalInput(15.0, 20.0, "low"),
            "camera": SignalInput(30.0, None, "none"),
            "bikes": ok(85.0, 100.0),
        },
        S,
    )
    assert res.used == ["bikes"] and res.score == 50.0
    traffic = res.signals["traffic"]
    assert traffic.status == "low_confidence"
    assert (traffic.change_pct, traffic.sub_score) == (-25.0, 62.5)  # shown, not used
    assert res.signals["camera"].status == "no_baseline"
    # Accepting low-confidence baselines (configurable) brings traffic back in.
    lenient = S.with_overrides(impact_min_confidence="low")
    assert impact_score({"traffic": SignalInput(15.0, 20.0, "low")}, lenient).score == 62.5


def test_zero_weight_and_custom_weights():
    s = Settings(impact_weight_traffic=0.0, impact_weight_camera=1.0, impact_weight_bikes=1.0)
    res = impact_score(
        {"traffic": ok(15.0, 20.0), "camera": ok(20.0, 20.0), "bikes": ok(70.0, 100.0)}, s
    )
    assert res.signals["traffic"].status == "zero_weight"
    assert res.used == ["camera", "bikes"]
    assert res.score == pytest.approx((0.0 + 100.0) / 2)


def test_no_usable_signal_gives_none_not_zero():
    res = impact_score({}, S)
    assert res.score is None and res.used == []
    assert {r.status for r in res.signals.values()} == {"no_data"}
    zero_baseline = impact_score({"bikes": ok(10.0, 0.0)}, S)
    assert zero_baseline.score is None
    assert zero_baseline.signals["bikes"].status == "no_baseline"
    assert impact_score({"traffic": ok(float("nan"), 20.0)}, S).score is None


def test_detail_is_json_friendly():
    detail = impact_score({"traffic": ok(15.0, 20.0)}, S).detail()
    assert set(detail) == {"traffic", "bus", "camera", "bikes"}
    assert detail["traffic"]["status"] == "used" and detail["traffic"]["effective_weight"] == 1.0


def test_impact_series_renormalizes_per_minute():
    series = pd.DataFrame(
        {
            "traffic_speed_cmp": [15.0, 15.0, None, 20.0],
            "traffic_baseline_mph": [20.0, 20.0, 20.0, 20.0],
            "bikes_cmp": [85.0, None, None, 100.0],
            "bikes_baseline": [100.0, 100.0, 100.0, 100.0],
        }
    )
    out = impact_series(series, ["traffic", "bikes"], S)
    assert out[0] == pytest.approx((0.4 * 62.5 + 0.3 * 50) / 0.7, abs=0.05)
    assert out[1] == 62.5  # bikes missing this minute: traffic alone
    assert math.isnan(out[2])  # nothing observed: unknown, not 0
    assert out[3] == 0.0
    # Signals not used for the event are ignored even if present.
    bikes_only = impact_series(series, ["bikes"], S)
    assert bikes_only[0] == 50.0 and math.isnan(bikes_only[1])


def test_surface_recovery_needs_a_full_quiet_run_after_the_peak():
    t = pd.date_range("2026-11-30 14:00", periods=30, freq="min")
    s = Settings(impact_recovery_threshold=20.0, impact_recovery_minutes=3)
    scores = pd.Series([80.0] * 5 + [10.0, 10.0, 50.0] + [10.0] * 3 + [5.0] * 19)
    assert surface_recovery(pd.Series(t), scores, t[0], s) == t[8]
    gaps = pd.Series([80.0] * 5 + [10.0, None, 10.0, None] + [None] * 21)
    assert surface_recovery(pd.Series(t), gaps, t[0], s) is None  # gaps break the run


def test_bus_signal_scores_slowdowns_and_renormalizes():
    assert sub_score("bus", -20.0, S) == 50.0 and sub_score("bus", 10.0, S) == 0.0
    res = impact_score({"traffic": ok(15.0, 20.0), "bus": ok(6.0, 8.0)}, S)
    # Both 25% slower -> 62.5 each; weights 0.4 and 0.3 renormalized.
    assert res.used == ["traffic", "bus"] and res.score == 62.5
    assert res.signals["bus"].effective_weight == pytest.approx(3 / 7, abs=1e-4)
    no_key = impact_score({"traffic": ok(15.0, 20.0), "bus": SignalInput(None, None)}, S)
    assert no_key.signals["bus"].status == "no_data" and no_key.used == ["traffic"]
