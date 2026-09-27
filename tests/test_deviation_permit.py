"""偏差累计许可：预算内照常走，超预算停住，复核结论放行。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.config import PermitLimits
from glassmelt.console.api import ConsoleAPI
from glassmelt.errors import ConfigError, InterlockError, SequenceError

TARGET = 186.0


def _drive(platform, mass_g: float, count: int):
    decision = None
    for _ in range(count):
        decision = platform.permit.observe(platform.gob.form(mass_g))
    return decision


def test_small_deviations_stay_within_budget(platform):
    decision = _drive(platform, TARGET - 1.0, 5)
    assert decision.held is False
    snap = platform.permit.snapshot()
    assert snap["cusum_short_g"] == 0.0
    assert snap["streak"] == 0
    assert platform.permit.held is False


def test_consecutive_short_gobs_trip_the_streak_limit(platform):
    decision = _drive(platform, TARGET - 2.0, 4)
    assert decision.held is True
    assert decision.trigger == "streak"
    assert decision.side == "short"
    # 每颗超额只有 0.5 g，累计离 6.0 g 预算还远，是"连着偏"触发的。
    assert platform.permit.snapshot()["cusum_short_g"] == pytest.approx(2.0)


def test_accumulated_shortfall_trips_the_budget(platform):
    _drive(platform, TARGET - 4.0, 2)
    decision = _drive(platform, TARGET, 1)
    assert decision.held is False
    decision = _drive(platform, TARGET - 4.0, 1)
    assert decision.held is True
    assert decision.trigger == "cusum"
    assert platform.permit.snapshot()["cusum_short_g"] == pytest.approx(6.0)


def test_heavy_side_is_watched_too(platform):
    _drive(platform, TARGET + 6.0, 1)
    decision = _drive(platform, TARGET + 6.0, 1)
    assert decision.held is True
    assert decision.side == "heavy"
    assert platform.permit.snapshot()["cusum_heavy_g"] == pytest.approx(9.0)


def test_held_permit_stops_forming_but_anneal_continues(running):
    for _ in range(3):
        running.cycle(CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=TARGET - 3.0, dt=1.0))
    cuts_before = running.shear.counts()["cuts"]
    summary = running.cycle(
        CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=TARGET - 3.0, dt=1.0)
    )
    assert summary["permit"]["held"] is True
    assert summary["permit"]["trigger"] == "cusum"
    assert summary["cut"] is None
    assert summary["forming"]["held"] is True
    assert running.shear.counts()["cuts"] == cuts_before
    # 停的是成型，退火结算照常走。
    assert summary["anneal"]["belt"]["speed_m_per_min"] > 0


def test_held_permit_never_releases_itself(platform):
    _drive(platform, TARGET - 3.0, 4)
    assert platform.permit.held is True
    _drive(platform, TARGET, 12)
    snap = platform.permit.snapshot()
    assert snap["cusum_short_g"] == 0.0
    assert platform.permit.held is True


def test_release_requires_held_state_operator_and_known_verdict(platform):
    with pytest.raises(SequenceError):
        platform.permit.release("张工", "probe_fault")
    _drive(platform, TARGET - 2.0, 4)
    assert platform.permit.held is True
    with pytest.raises(SequenceError):
        platform.permit.release("", "probe_fault")
    with pytest.raises(SequenceError):
        platform.permit.release("张工", "guess")
    assert platform.permit.held is True


def test_release_resets_ledger_and_forming_resumes(running):
    for _ in range(4):
        running.cycle(CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=TARGET - 3.0, dt=1.0))
    assert running.permit.held is True
    payload = running.release_permit("张工", "real_deviation", "供料道温度已调回")
    assert payload["released"] is True
    assert payload["release"]["operator"] == "张工"
    snap = running.permit.snapshot()
    assert snap["held"] is False
    assert snap["cusum_short_g"] == 0.0
    assert snap["streak"] == 0
    summary = running.cycle(
        CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=TARGET, dt=1.0)
    )
    assert summary["cut"]["gob_index"] > 0
    assert summary["forming"]["filled"] is True


def test_blow_mold_is_blocked_while_held(running):
    for _ in range(4):
        running.cycle(CycleInputs(load=0.8, pull_rate=0.9, gob_mass_g=TARGET - 3.0, dt=1.0))
    assert running.permit.held is True
    with pytest.raises(InterlockError):
        running.blow_mold(path="cycle")
    with pytest.raises(InterlockError):
        running.blow_mold(path="retry", reason="补料")


def test_hold_is_raised_once_until_release(platform):
    _drive(platform, TARGET - 2.0, 6)
    assert platform.permit.held is True
    epoch = platform.permit.epoch
    holds = [e for e in platform.audit.entries(500) if e["action"] == "permit_held"]
    assert len(holds) == 1
    assert platform.permit.epoch == epoch


def test_verdicts_are_counted_and_audited(platform):
    _drive(platform, TARGET - 2.0, 4)
    platform.permit.release("张工", "probe_fault", "换探头后比对正常")
    _drive(platform, TARGET - 2.0, 4)
    platform.permit.release("李工", "real_deviation", "供料道已调温")
    counts = platform.permit.counts()
    assert counts == {"holds": 2, "releases": 2, "probe_faults": 1, "real_deviations": 1}
    entries = platform.audit.entries(500)
    released = [e for e in entries if e["action"] == "permit_released"]
    assert len(released) == 2
    assert released[0]["detail"]["operator"] == "张工"
    assert released[0]["detail"]["verdict"] == "probe_fault"
    assert released[1]["detail"]["operator"] == "李工"
    assert released[1]["detail"]["verdict"] == "real_deviation"


def test_console_release_endpoint(platform):
    api = ConsoleAPI(platform)
    _drive(platform, TARGET - 2.0, 4)
    bad = api.release_permit({"operator": "", "verdict": "probe_fault"})
    assert bad["ok"] is False
    good = api.release_permit(
        {"operator": "张工", "verdict": "probe_fault", "note": "比对正常"}
    )
    assert good["ok"] is True
    assert good["result"]["release"]["operator"] == "张工"
    assert platform.permit.held is False


def test_permit_limits_validation():
    with pytest.raises(ConfigError):
        PermitLimits.from_mapping(
            {"drift_allow_g": 1.5, "budget_g": 0.0, "streak_max": 4}
        )
    with pytest.raises(ConfigError):
        PermitLimits.from_mapping({"drift_allow_g": 1.5, "budget_g": 6.0})
    with pytest.raises(ConfigError):
        PermitLimits.from_mapping(
            {"drift_allow_g": -0.1, "budget_g": 6.0, "streak_max": 4}
        )
