"""成型循环。

成型循环的四个动作是「料滴成型 → 合模 → 吹气 → 脱模」，正常循环与补料
重试都按这个顺序走。循环能不能进入下一拍，要看合模机构当下的状态，不能
拿重开模之前的结论当依据——模具重开后还是照旧吹气，玻璃液会从分模缝喷
出来。进入循环前还要确认偏差累计许可没有停住：许可停住期间，正常循环与
补料重试都不得合模，等人工复核放行。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class CycleReady:
    """一次循环准备判定。"""

    ready: bool
    closure_epoch: int
    closed: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "closure_epoch": self.closure_epoch,
            "closed": self.closed,
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class CycleResult:
    """一个完整成型循环的结果。"""

    path: str
    gob_index: int
    closed: bool
    blown_ms: float
    filled: bool
    closure_epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "gob_index": self.gob_index,
            "closed": self.closed,
            "blown_ms": round(self.blown_ms, 2),
            "filled": self.filled,
            "closure_epoch": self.closure_epoch,
            "at": round(self.at, 3),
        }


class FormingCycle:
    """成型循环执行器。"""

    def __init__(self, bus: EventBus, permit) -> None:
        self._bus = bus
        self._permit = permit
        self._cycles = 0
        self._retries = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    @property
    def cycles(self) -> int:
        with self._lock:
            return self._cycles

    def ready(self, closer) -> CycleReady:
        """现场判定当前合模机构是否允许进入下一拍。"""

        closure = closer.closure()
        return CycleReady(
            ready=closure.closed,
            closure_epoch=closure.epoch,
            closed=closure.closed,
            at=closure.at,
        )

    def run(self, gob, closer, blow_station, filler) -> CycleResult:
        """正常成型循环：合模 → 吹气 → 灌模 → 脱模。"""

        return self._execute(
            gob, closer, blow_station, filler, path="cycle", reason="正常循环"
        )

    def retry(self, gob, closer, blow_station, filler, reason: str) -> CycleResult:
        """补料重试：重开模再合模，然后走同一条吹气前置检查。"""

        closer.open_mold(f"成形重试：{reason}")
        with self._lock:
            self._retries += 1
        return self._execute(
            gob, closer, blow_station, filler, path="retry", reason=reason
        )

    def _execute(
        self, gob, closer, blow_station, filler, path: str, reason: str
    ) -> CycleResult:
        self._permit.require_clear(gob)
        closure = closer.close()
        readiness = self.ready(closer)
        if not readiness.ready:
            raise InterlockError(
                f"合模未到位（代次 {readiness.closure_epoch}），禁止吹气",
                component="feeder",
            )
        if path == "cycle":
            blow = blow_station.normal(closure)
        else:
            blow = blow_station.retry(closure, reason)
        fill = filler.fill(gob)
        closer.open_mold("脱模")
        with self._lock:
            self._cycles += 1
            result = CycleResult(
                path=path,
                gob_index=gob.index,
                closed=closure.closed,
                blown_ms=blow.blown_ms,
                filled=fill.filled,
                closure_epoch=closure.epoch,
                at=gob.at,
            )
            self._history.append(result.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "feeder",
                "feeder",
                "cycle_done",
                subject="forming_cycle",
                severity="notice",
                path=path,
                gob_index=gob.index,
                closure_epoch=closure.epoch,
                blown_ms=round(blow.blown_ms, 2),
                filled=fill.filled,
            )
        )
        return result

    def counts(self) -> dict:
        with self._lock:
            return {"cycles": self._cycles, "retries": self._retries}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "cycles": counts["cycles"],
                "retries": counts["retries"],
                "recent": list(self._history[-3:]),
            }
