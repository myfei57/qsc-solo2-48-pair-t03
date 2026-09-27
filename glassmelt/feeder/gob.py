"""料滴成型判定。

料滴从供料道流出后要先"成型到位"，剪切才允许动作。成型与否必须按当前
这一颗料滴的质量重新判：上一颗料滴的成型结论对下一颗没有意义。把确认
结论缓存下来跨料滴复用，剪切就会切到还没成型的料滴，切出来偏短。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import FeederLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class Gob:
    """一颗料滴。"""

    index: int
    mass_g: float
    formed: bool
    epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "mass_g": round(self.mass_g, 3),
            "formed": self.formed,
            "epoch": self.epoch,
            "at": round(self.at, 3),
        }

    def is_formed(self) -> bool:
        return bool(self.formed)


class GobFormer:
    """料滴成型判定。"""

    def __init__(self, clock, bus: EventBus, limits: FeederLimits, permit) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._permit = permit
        self._index = 0
        self._epoch = 0
        self._confirmation: Gob | None = None
        self._formed_count = 0
        self._rejected_count = 0
        self._lock = threading.RLock()

    @property
    def epoch(self) -> int:
        """料滴代次；每成型一颗加一。"""

        with self._lock:
            return self._epoch

    def form(self, mass_g: float) -> Gob:
        """现场判定一颗料滴是否成型到位，并把它记为最新确认。"""

        with self._lock:
            self._index += 1
            self._epoch += 1
            formed = self._limits.mass_ok(mass_g)
            gob = Gob(
                index=self._index,
                mass_g=float(mass_g),
                formed=formed,
                epoch=self._epoch,
                at=self._clock.now(),
            )
            if formed:
                self._formed_count += 1
            else:
                self._rejected_count += 1
            self._confirmation = gob
        self._bus.publish(
            Event.of(
                "feeder",
                "feeder",
                "gob_form_checked",
                subject="gob",
                severity="info" if formed else "warning",
                index=gob.index,
                mass_g=round(gob.mass_g, 3),
                formed=gob.formed,
                epoch=gob.epoch,
            )
        )
        # 判成型的料滴才进偏差累计；未成型的由剪切与灌模的逐颗联锁拦下。
        if gob.is_formed():
            self._permit.observe(gob)
        return gob

    def recover_mass(self) -> float:
        """恢复成型用的补偿质量。"""

        return self._limits.recover_gob_mass_g

    def last_confirmation(self) -> Gob | None:
        """返回最近一次成型确认（仅供展示与审计，不代替现场判定）。"""

        with self._lock:
            return self._confirmation

    def counts(self) -> dict:
        with self._lock:
            return {
                "formed": self._formed_count,
                "rejected": self._rejected_count,
                "gobs": self._index,
            }

    def snapshot(self) -> dict:
        confirmation = self.last_confirmation()
        counts = self.counts()
        with self._lock:
            return {
                "gobs": counts["gobs"],
                "formed": counts["formed"],
                "rejected": counts["rejected"],
                "target_g": self._limits.gob_mass_target_g,
                "tolerance_g": self._limits.gob_mass_tolerance_g,
                "epoch": self._epoch,
                "last_confirmation": confirmation.as_dict() if confirmation else None,
            }
