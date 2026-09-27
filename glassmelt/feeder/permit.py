"""料滴偏差累计许可。

逐颗公差判定管不住"每颗都在公差内、但连着偏、慢慢偏"：单颗看都合格，
瓶子却成批欠重。许可把每颗判成型料滴的偏差按方向累计（偏短一侧、偏重
一侧），正常波动会被后续料滴抵消，持续的单向偏差则不断累积；任一方向
超过预算就把成型停住，等人工复核。

停住只能由人工复核解除：复核必须明确结论是探头问题还是真实偏差，许可
状态落盘保存——重启平台、推进周期、偏差自己回落，都不会把许可偷偷
放行。未判成型的料滴由剪切与灌模的逐颗联锁拦下，不进入累计。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import FeederLimits, PermitLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore

#: 许可状态的落盘键。
PERMIT_KEY = "feeder_deviation_permit"

#: 复核结论的合法取值：探头问题（probe_fault）或真实偏差（real_deviation）。
VERDICTS = ("probe_fault", "real_deviation")

_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class PermitHalt:
    """一次停住记录。"""

    epoch: int
    gob_index: int
    reason: str
    short_g: float
    over_g: float
    at: float

    def as_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "gob_index": self.gob_index,
            "reason": self.reason,
            "short_g": round(self.short_g, 3),
            "over_g": round(self.over_g, 3),
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class PermitReview:
    """一次人工复核记录。"""

    epoch: int
    halt_epoch: int
    verdict: str
    reviewer: str
    note: str
    at: float

    def as_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "halt_epoch": self.halt_epoch,
            "verdict": self.verdict,
            "reviewer": self.reviewer,
            "note": self.note,
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class PermitDecision:
    """一颗料滴进入累计后的许可结论。"""

    gob_index: int
    deviation_g: float
    short_g: float
    over_g: float
    allowed: bool
    halted: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "gob_index": self.gob_index,
            "deviation_g": round(self.deviation_g, 3),
            "short_g": round(self.short_g, 3),
            "over_g": round(self.over_g, 3),
            "allowed": self.allowed,
            "halted": self.halted,
            "at": round(self.at, 3),
        }


class DeviationPermit:
    """料滴偏差累计许可：预算内照常走，超预算停住等复核。"""

    def __init__(
        self,
        clock,
        bus: EventBus,
        store: JsonStore,
        feeder: FeederLimits,
        limits: PermitLimits,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._store = store
        self._feeder = feeder
        self._limits = limits
        self._short_g = 0.0
        self._over_g = 0.0
        self._halted = False
        self._halt: PermitHalt | None = None
        self._last_review: PermitReview | None = None
        self._epoch = 0
        self._observations = 0
        self._halts = 0
        self._reviews = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()
        self._restore()

    def observe(self, gob) -> PermitDecision:
        """把一颗判成型料滴的偏差记入累计，任一方向超预算即停住。

        停住期间继续记录偏差（复核要看最新读数），但许可保持停住：
        偏差回落、继续观测都不会让许可自己放行。
        """

        deviation = float(gob.mass_g) - self._feeder.gob_mass_target_g
        allowance = self._limits.allowance_g
        halt: PermitHalt | None = None
        with self._lock:
            self._observations += 1
            self._short_g = max(0.0, self._short_g - deviation - allowance)
            self._over_g = max(0.0, self._over_g + deviation - allowance)
            if not self._halted:
                breached = []
                if self._short_g > self._limits.budget_g:
                    breached.append("偏短")
                if self._over_g > self._limits.budget_g:
                    breached.append("偏重")
                if breached:
                    self._epoch += 1
                    self._halts += 1
                    reason = (
                        f"{'、'.join(breached)}侧累计偏差超预算"
                        f"（偏短 {self._short_g:.1f} g／偏重 {self._over_g:.1f} g，"
                        f"预算 {self._limits.budget_g:.1f} g）"
                    )
                    self._halt = PermitHalt(
                        epoch=self._epoch,
                        gob_index=gob.index,
                        reason=reason,
                        short_g=self._short_g,
                        over_g=self._over_g,
                        at=self._clock.now(),
                    )
                    self._halted = True
                    halt = self._halt
            decision = PermitDecision(
                gob_index=gob.index,
                deviation_g=deviation,
                short_g=self._short_g,
                over_g=self._over_g,
                allowed=not self._halted,
                halted=self._halted,
                at=self._clock.now(),
            )
            self._history.append(decision.as_dict())
            if len(self._history) > _HISTORY_LIMIT:
                del self._history[: len(self._history) - _HISTORY_LIMIT]
            payload = self._persist_payload()
        self._store.write(PERMIT_KEY, payload)
        if halt is not None:
            self._bus.publish(
                Event.of(
                    "feeder",
                    "feeder",
                    "permit_halted",
                    subject="deviation_permit",
                    severity="alarm",
                    epoch=halt.epoch,
                    gob_index=halt.gob_index,
                    reason=halt.reason,
                    short_g=round(halt.short_g, 3),
                    over_g=round(halt.over_g, 3),
                    budget_g=self._limits.budget_g,
                )
            )
        return decision

    def is_halted(self) -> bool:
        """许可当前是否停住。"""

        with self._lock:
            return self._halted

    def peak_g(self) -> float:
        """两侧累计偏差中较高的一侧，供趋势采样。"""

        with self._lock:
            return max(self._short_g, self._over_g)

    def require_clear(self, gob=None) -> None:
        """确认许可未停住；停住期间任何成型动作都被拒绝。"""

        with self._lock:
            if not self._halted:
                return
            halt = self._halt
        reason = halt.reason if halt else "累计偏差超预算"
        where = f"（第 {gob.index} 颗料滴）" if gob is not None else ""
        raise InterlockError(
            f"偏差累计许可停住中{where}：{reason}，人工复核放行前禁止成型",
            component="feeder",
        )

    def review(self, verdict: str, reviewer: str, note: str = "") -> PermitReview:
        """人工复核：确认探头问题还是真实偏差，然后放行并清零累计。"""

        if verdict not in VERDICTS:
            raise ValueError(
                f"未知的复核结论: {verdict!r}（可选: {'、'.join(VERDICTS)}）"
            )
        reviewer = str(reviewer).strip()
        if not reviewer:
            raise ValueError("复核人不能为空")
        with self._lock:
            if not self._halted:
                raise InterlockError("偏差累计许可未停住，无需复核", component="feeder")
            halt_epoch = self._halt.epoch if self._halt else self._epoch
            self._halted = False
            self._halt = None
            self._short_g = 0.0
            self._over_g = 0.0
            self._epoch += 1
            self._reviews += 1
            review = PermitReview(
                epoch=self._epoch,
                halt_epoch=halt_epoch,
                verdict=verdict,
                reviewer=reviewer,
                note=str(note),
                at=self._clock.now(),
            )
            self._last_review = review
            payload = self._persist_payload()
        self._store.write(PERMIT_KEY, payload)
        self._bus.publish(
            Event.of(
                "feeder",
                "feeder",
                "permit_released",
                subject="deviation_permit",
                severity="notice",
                epoch=review.epoch,
                halt_epoch=review.halt_epoch,
                verdict=review.verdict,
                reviewer=review.reviewer,
                note=review.note,
            )
        )
        return review

    def is_durable(self) -> bool:
        """许可状态是否已落盘且与内存一致。"""

        snapshot = self._store.read(PERMIT_KEY)
        if not snapshot:
            return False
        with self._lock:
            return (
                int(snapshot.get("epoch", -1)) == self._epoch
                and bool(snapshot.get("halted", False)) == self._halted
            )

    def counts(self) -> dict:
        with self._lock:
            return {
                "observations": self._observations,
                "halts": self._halts,
                "reviews": self._reviews,
                "epoch": self._epoch,
            }

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "halted": self._halted,
                "epoch": counts["epoch"],
                "short_g": round(self._short_g, 3),
                "over_g": round(self._over_g, 3),
                "budget_g": self._limits.budget_g,
                "allowance_g": self._limits.allowance_g,
                "observations": counts["observations"],
                "halts": counts["halts"],
                "reviews": counts["reviews"],
                "durable": self.is_durable(),
                "halt": self._halt.as_dict() if self._halt else None,
                "last_review": self._last_review.as_dict() if self._last_review else None,
                "recent": self.history(3),
            }

    def _persist_payload(self) -> dict:
        """组装落盘内容；调用方必须持有锁。"""

        return {
            "halted": self._halted,
            "epoch": self._epoch,
            "short_g": round(self._short_g, 4),
            "over_g": round(self._over_g, 4),
            "observations": self._observations,
            "halts": self._halts,
            "reviews": self._reviews,
            "halt": self._halt.as_dict() if self._halt else None,
            "updated_at": round(self._clock.now(), 3),
        }

    def _restore(self) -> None:
        """从落盘状态恢复许可：停住状态不随重启消失。"""

        snapshot = self._store.read(PERMIT_KEY)
        if not snapshot:
            return
        self._halted = bool(snapshot.get("halted", False))
        self._epoch = int(snapshot.get("epoch", 0))
        self._short_g = float(snapshot.get("short_g", 0.0))
        self._over_g = float(snapshot.get("over_g", 0.0))
        self._observations = int(snapshot.get("observations", 0))
        self._halts = int(snapshot.get("halts", 0))
        self._reviews = int(snapshot.get("reviews", 0))
        halt = snapshot.get("halt")
        if self._halted and isinstance(halt, dict):
            self._halt = PermitHalt(
                epoch=int(halt.get("epoch", self._epoch)),
                gob_index=int(halt.get("gob_index", 0)),
                reason=str(halt.get("reason", "累计偏差超预算")),
                short_g=float(halt.get("short_g", 0.0)),
                over_g=float(halt.get("over_g", 0.0)),
                at=float(halt.get("at", 0.0)),
            )
        if self._halted:
            self._bus.publish(
                Event.of(
                    "feeder",
                    "feeder",
                    "permit_restored_halted",
                    subject="deviation_permit",
                    severity="warning",
                    epoch=self._epoch,
                    reason=self._halt.reason if self._halt else "累计偏差超预算",
                )
            )
