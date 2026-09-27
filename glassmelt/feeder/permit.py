"""料滴偏差累计许可。

单颗料滴偏短一点不算事故，怕的是连着偏、或一段时间里累计偏得太多——等
人工翻记录发现时，成批的瓶子已经出去了。许可按颗记账：每颗偏差先扣掉
豁免额，余下的偏短侧与偏重侧分别累计（CUSUM）；任一侧累计超预算、或
同向连续超限颗数到顶，许可立即置停，成型停在当场等人复核。

停住之后没有任何自动放行路径：偏差自己回到预算内，许可也不动。唯一出口
是复核人带着结论调 :meth:`DeviationPermit.release`——结论要么"探头问题"
（读数不可信，账本作废重计），要么"真的偏了"（处置供料后重新累计），
两种结论都连人带时间落审计，事后能分清每次停线冤不冤。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import FeederLimits, PermitLimits
from ..errors import InterlockError, SequenceError
from ..eventbus import Event, EventBus

#: 复核结论：称重探头问题，停线期间的读数不可信。
VERDICT_PROBE_FAULT = "probe_fault"
#: 复核结论：料滴真的偏了，供料侧已处置。
VERDICT_REAL_DEVIATION = "real_deviation"
#: 合法的复核结论集合，放行时结论必须落在这里面。
VERDICTS = (VERDICT_PROBE_FAULT, VERDICT_REAL_DEVIATION)

_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class PermitDecision:
    """一颗料滴入账后的许可判定。"""

    gob_index: int
    deviation_g: float
    side: str
    cusum_short_g: float
    cusum_heavy_g: float
    streak: int
    held: bool
    trigger: str
    at: float

    def as_dict(self) -> dict:
        return {
            "gob_index": self.gob_index,
            "deviation_g": round(self.deviation_g, 3),
            "side": self.side,
            "cusum_short_g": round(self.cusum_short_g, 3),
            "cusum_heavy_g": round(self.cusum_heavy_g, 3),
            "streak": self.streak,
            "held": self.held,
            "trigger": self.trigger,
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class HoldRecord:
    """一次许可置停。"""

    trigger: str
    side: str
    gob_index: int
    epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "side": self.side,
            "gob_index": self.gob_index,
            "epoch": self.epoch,
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class ReleaseRecord:
    """一次人工复核放行。"""

    operator: str
    verdict: str
    note: str
    epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "operator": self.operator,
            "verdict": self.verdict,
            "note": self.note,
            "epoch": self.epoch,
            "at": round(self.at, 3),
        }


class DeviationPermit:
    """偏差累计许可：预算内照常走，超预算停住等复核。"""

    def __init__(
        self,
        clock,
        bus: EventBus,
        limits: PermitLimits,
        feeder: FeederLimits,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._feeder = feeder
        self._held = False
        self._epoch = 0
        self._cusum_short = 0.0
        self._cusum_heavy = 0.0
        self._streak = 0
        self._streak_side = "on_target"
        self._holds = 0
        self._releases = 0
        self._probe_faults = 0
        self._real_deviations = 0
        self._hold_history: list[HoldRecord] = []
        self._release_history: list[ReleaseRecord] = []
        self._lock = threading.RLock()

    @property
    def held(self) -> bool:
        """许可是否处于停住状态。"""

        with self._lock:
            return self._held

    @property
    def epoch(self) -> int:
        """许可代次；每次置停或放行后加一。"""

        with self._lock:
            return self._epoch

    def observe(self, gob) -> PermitDecision:
        """把一颗料滴的偏差入账，返回入账后的许可判定。"""

        deviation = float(gob.mass_g) - self._feeder.gob_mass_target_g
        allow = self._limits.drift_allow_g
        hold: HoldRecord | None = None
        with self._lock:
            self._cusum_short = max(0.0, self._cusum_short - deviation - allow)
            self._cusum_heavy = max(0.0, self._cusum_heavy + deviation - allow)
            if deviation < -allow:
                side = "short"
                self._streak = self._streak + 1 if self._streak_side == "short" else 1
                self._streak_side = "short"
            elif deviation > allow:
                side = "heavy"
                self._streak = self._streak + 1 if self._streak_side == "heavy" else 1
                self._streak_side = "heavy"
            else:
                side = "on_target"
                self._streak = 0
                self._streak_side = "on_target"
            trigger = "none"
            if not self._held:
                if (
                    self._cusum_short >= self._limits.budget_g
                    or self._cusum_heavy >= self._limits.budget_g
                ):
                    trigger = "cusum"
                elif self._streak >= self._limits.streak_max:
                    trigger = "streak"
                if trigger != "none":
                    self._held = True
                    self._epoch += 1
                    self._holds += 1
                    hold = HoldRecord(
                        trigger=trigger,
                        side=side,
                        gob_index=gob.index,
                        epoch=self._epoch,
                        at=self._clock.now(),
                    )
                    self._hold_history.append(hold)
                    if len(self._hold_history) > _HISTORY_LIMIT:
                        del self._hold_history[: len(self._hold_history) - _HISTORY_LIMIT]
            decision = PermitDecision(
                gob_index=gob.index,
                deviation_g=deviation,
                side=side,
                cusum_short_g=self._cusum_short,
                cusum_heavy_g=self._cusum_heavy,
                streak=self._streak,
                held=self._held,
                trigger=trigger,
                at=self._clock.now(),
            )
        if hold is not None:
            self._bus.publish(
                Event.of(
                    "feeder",
                    "feeder",
                    "permit_held",
                    subject="deviation_permit",
                    severity="alarm",
                    trigger=hold.trigger,
                    side=hold.side,
                    gob_index=hold.gob_index,
                    deviation_g=round(decision.deviation_g, 3),
                    cusum_short_g=round(decision.cusum_short_g, 3),
                    cusum_heavy_g=round(decision.cusum_heavy_g, 3),
                    streak=decision.streak,
                    budget_g=self._limits.budget_g,
                    epoch=hold.epoch,
                )
            )
        return decision

    def require_clear(self) -> None:
        """硬门控：许可停住期间禁止进入成型动作。"""

        if self.held:
            raise InterlockError(
                "料滴偏差累计超预算，成型已停住，待人工复核放行",
                component="feeder",
            )

    def release(self, operator: str, verdict: str, note: str = "") -> ReleaseRecord:
        """人工复核放行：登记复核人与结论，账本清零后解除停住。"""

        operator = str(operator).strip()
        if not operator:
            raise SequenceError("放行必须登记复核人", component="feeder")
        if verdict not in VERDICTS:
            raise SequenceError(f"未知的复核结论: {verdict}", component="feeder")
        with self._lock:
            if not self._held:
                raise SequenceError("许可未处于停住状态，无需放行", component="feeder")
            self._held = False
            self._epoch += 1
            self._releases += 1
            if verdict == VERDICT_PROBE_FAULT:
                self._probe_faults += 1
            else:
                self._real_deviations += 1
            self._cusum_short = 0.0
            self._cusum_heavy = 0.0
            self._streak = 0
            self._streak_side = "on_target"
            record = ReleaseRecord(
                operator=operator,
                verdict=verdict,
                note=str(note),
                epoch=self._epoch,
                at=self._clock.now(),
            )
            self._release_history.append(record)
            if len(self._release_history) > _HISTORY_LIMIT:
                del self._release_history[: len(self._release_history) - _HISTORY_LIMIT]
        self._bus.publish(
            Event.of(
                "feeder",
                "feeder",
                "permit_released",
                subject="deviation_permit",
                severity="notice",
                operator=record.operator,
                verdict=record.verdict,
                note=record.note,
                epoch=record.epoch,
            )
        )
        return record

    def counts(self) -> dict:
        with self._lock:
            return {
                "holds": self._holds,
                "releases": self._releases,
                "probe_faults": self._probe_faults,
                "real_deviations": self._real_deviations,
            }

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "held": self._held,
                "epoch": self._epoch,
                "cusum_short_g": round(self._cusum_short, 3),
                "cusum_heavy_g": round(self._cusum_heavy, 3),
                "streak": self._streak,
                "streak_side": self._streak_side,
                "drift_allow_g": self._limits.drift_allow_g,
                "budget_g": self._limits.budget_g,
                "streak_max": self._limits.streak_max,
                "holds": counts["holds"],
                "releases": counts["releases"],
                "probe_faults": counts["probe_faults"],
                "real_deviations": counts["real_deviations"],
                "last_hold": self._hold_history[-1].as_dict()
                if self._hold_history
                else None,
                "last_release": self._release_history[-1].as_dict()
                if self._release_history
                else None,
            }
