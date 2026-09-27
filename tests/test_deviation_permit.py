"""偏差累计许可：预算内照常走，超预算停住，人工复核后才放行。"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request

import pytest

from glassmelt.app import CycleInputs, build_platform
from glassmelt.config import PlatformConfig, config_to_mapping, default_config
from glassmelt.console.server import ConsoleServer
from glassmelt.errors import ConfigError, InterlockError
from glassmelt.feeder.permit import PERMIT_KEY


def _form_short(platform, count: int, short_by: float = 3.0) -> None:
    target = platform.config.feeder.gob_mass_target_g
    for _ in range(count):
        platform.gob.form(target - short_by)


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_occasional_short_gob_stays_clear(platform):
    target = platform.config.feeder.gob_mass_target_g
    for _ in range(5):
        _form_short(platform, 1)
        for _ in range(3):
            platform.gob.form(target)
    assert platform.permit.is_halted() is False
    assert platform.permit.peak_g() == pytest.approx(0.0)


def test_unformed_gobs_do_not_feed_the_accumulator(platform):
    platform.gob.form(120.0)
    platform.gob.form(100.0)
    assert platform.permit.is_halted() is False
    assert platform.permit.snapshot()["observations"] == 0


def test_consecutive_short_gobs_halt_the_permit(running):
    _form_short(running, 8)
    assert running.permit.is_halted() is True
    snapshot = running.permit.snapshot()
    assert snapshot["halted"] is True
    assert snapshot["halts"] == 1
    assert snapshot["short_g"] > running.config.permit.budget_g
    assert "偏短" in snapshot["halt"]["reason"]
    assert snapshot["durable"] is True


def test_consecutive_overweight_gobs_halt_the_permit(platform):
    target = platform.config.feeder.gob_mass_target_g
    for _ in range(8):
        platform.gob.form(target + 3.0)
    snapshot = platform.permit.snapshot()
    assert snapshot["halted"] is True
    assert snapshot["over_g"] > platform.config.permit.budget_g
    assert "偏重" in snapshot["halt"]["reason"]


def test_halted_permit_blocks_every_forming_path(running):
    _form_short(running, 8)
    gob = running.gob.form(running.config.feeder.gob_mass_target_g)
    with pytest.raises(InterlockError):
        running.forming.run(gob, running.closer, running.blow, running.filler)
    with pytest.raises(InterlockError):
        running.forming.retry(gob, running.closer, running.blow, running.filler, "补料")
    with pytest.raises(InterlockError):
        running.blow_mold(path="cycle")


def test_halt_never_releases_itself(running):
    _form_short(running, 8)
    assert running.permit.is_halted() is True
    target = running.config.feeder.gob_mass_target_g
    for _ in range(30):
        running.gob.form(target)
    assert running.permit.peak_g() == pytest.approx(0.0)
    assert running.permit.is_halted() is True
    gob = running.gob.form(target)
    with pytest.raises(InterlockError):
        running.forming.run(gob, running.closer, running.blow, running.filler)


def test_review_releases_and_clears_accumulators(running):
    _form_short(running, 8)
    result = running.review_forming_permit("probe_fault", "李工", "更换称重探头")
    assert result["review"]["verdict"] == "probe_fault"
    assert result["review"]["reviewer"] == "李工"
    assert result["permit"]["halted"] is False
    assert running.permit.peak_g() == pytest.approx(0.0)
    gob = running.gob.form(running.config.feeder.gob_mass_target_g)
    cycle = running.forming.run(gob, running.closer, running.blow, running.filler)
    assert cycle.closed is True
    assert running.permit.snapshot()["reviews"] == 1


def test_real_deviation_verdict_also_releases(running):
    _form_short(running, 8)
    result = running.review_forming_permit("real_deviation", "王工", "调整冲程后复测合格")
    assert result["review"]["verdict"] == "real_deviation"
    assert result["permit"]["halted"] is False


def test_review_requires_halt_verdict_and_reviewer(running):
    with pytest.raises(InterlockError):
        running.review_forming_permit("probe_fault", "李工")
    _form_short(running, 8)
    with pytest.raises(ValueError):
        running.review_forming_permit("maybe", "李工")
    with pytest.raises(ValueError):
        running.review_forming_permit("probe_fault", "   ")
    assert running.permit.is_halted() is True


def test_halt_survives_restart(tmp_path, clock):
    config = default_config()
    storage = str(tmp_path / "state")
    first = build_platform(config, clock=clock, storage_root=storage)
    _form_short(first, 8)
    assert first.permit.is_halted() is True
    assert first.store.exists(PERMIT_KEY) is True
    second = build_platform(config, clock=clock, storage_root=storage)
    assert second.permit.is_halted() is True
    assert second.permit.snapshot()["short_g"] > config.permit.budget_g
    result = second.review_forming_permit("real_deviation", "王工", "复核后调整冲程")
    assert result["permit"]["halted"] is False
    third = build_platform(config, clock=clock, storage_root=storage)
    assert third.permit.is_halted() is False


def test_cycle_skips_forming_while_halted_and_resumes_after_review(running):
    inputs = CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=183.0, dt=1.0)
    result = None
    for _ in range(8):
        result = running.cycle(inputs)
    assert result["forming_halted"] is True
    assert result["forming"] is None
    assert result["permit"]["halted"] is True
    assert result["anneal"]
    alarms = running.audit.alarms()
    assert any(item["action"] == "permit_halted" for item in alarms)
    running.review_forming_permit("real_deviation", "李工", "调整供料机冲程")
    resumed = running.cycle(
        CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0)
    )
    assert resumed["forming_halted"] is False
    assert resumed["forming"]["filled"] is True


def test_halt_and_release_are_audited(running):
    _form_short(running, 8)
    running.review_forming_permit("probe_fault", "李工")
    actions = [entry["action"] for entry in running.audit.entries(500)]
    assert "permit_halted" in actions
    assert "permit_released" in actions
    alarms = running.audit.alarms()
    assert any(
        item["action"] == "permit_halted" and item["severity"] == "alarm"
        for item in alarms
    )


def test_console_permit_review_flow(platform):
    server = ConsoleServer(platform)
    base = server.start()
    try:
        status, payload = _post(
            f"{base}/api/permit/review", {"verdict": "probe_fault", "reviewer": "李工"}
        )
        assert status == 200 and payload["ok"] is False
        _form_short(platform, 8)
        with urllib.request.urlopen(f"{base}/api/permit", timeout=5.0) as response:
            permit_state = json.loads(response.read().decode("utf-8"))
        assert permit_state["halted"] is True
        status, payload = _post(
            f"{base}/api/permit/review", {"verdict": "maybe", "reviewer": "李工"}
        )
        assert status == 400
        status, payload = _post(
            f"{base}/api/permit/review",
            {"verdict": "real_deviation", "reviewer": "王工", "note": "调整冲程"},
        )
        assert status == 200 and payload["ok"] is True
        assert payload["result"]["permit"]["halted"] is False
    finally:
        server.stop()


def test_permit_config_is_validated():
    mapping = config_to_mapping(default_config())
    missing = copy.deepcopy(mapping)
    del missing["limits"]["permit"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(missing)
    bad = copy.deepcopy(mapping)
    bad["limits"]["permit"]["allowance_g"] = 99.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(bad)
