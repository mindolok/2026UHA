"""Safe, reusable state-level pruning primitives for a future live router.

These are deterministic rules, not estimates. The caller must provide an
admissible remaining-time lower bound and matching service/trip context.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class StateLabel:
    stop_id: str
    current_line_id: str | None
    direction: str | None
    current_trip_id: str | None
    next_stop_seq: int | None
    destination_key: str
    event_date: str
    elapsed_minutes: float
    transfers: int
    walk_distance_m: float
    root_id: str

    def comparable_key(self, baseline_mode: str) -> tuple:
        if baseline_mode not in {"global_fastest", "per_root"}:
            raise ValueError("A team-approved baseline mode is required")
        return (
            self.stop_id,self.current_line_id,self.direction,
            self.current_trip_id,self.next_stop_seq,self.destination_key,
            self.event_date,self.root_id if baseline_mode=="per_root" else None,
        )


def prune_by_budget(elapsed_minutes:float,
                    admissible_remaining_minutes:float|None,
                    reference_minutes:float,
                    max_extra_minutes:float=60) -> bool:
    """Prune ONLY with a mathematically guaranteed remaining-time lower bound.

    If no such bound is available, use 0 (safe but less efficient). Never
    assume `straight_line_distance / 25 km/h` is admissible unless the
    transit provider proves 25 km/h is a maximum feasible speed.
    """
    if elapsed_minutes < 0 or reference_minutes < 0 or max_extra_minutes < 0:
        raise ValueError("Negative duration is invalid")
    if admissible_remaining_minutes is None:
        admissible_remaining_minutes = 0.0
    if admissible_remaining_minutes < 0:
        raise ValueError("Lower bound cannot be negative")
    return elapsed_minutes + admissible_remaining_minutes > reference_minutes + max_extra_minutes


class DominanceIndex:
    """Pareto dominance only between genuinely equivalent path states.

    The exact trip context and next_stop_seq are included in the key to
    prevent pruning paths that ride different buses from the same stop.
    Waiting to the later state is assumed physically possible; otherwise
    dominance must not be used by the caller.
    """
    def __init__(self,baseline_mode:str):
        self.baseline_mode=baseline_mode
        self.labels:dict[tuple,list[tuple[float,int,float]]]={}

    def dominated_or_add(self,state:StateLabel) -> bool:
        key=state.comparable_key(self.baseline_mode)
        labels=self.labels.get(key,[])
        here=(state.elapsed_minutes,state.transfers,state.walk_distance_m)
        if any(all(a<=b for a,b in zip(other,here)) for other in labels):
            return True
        labels=[x for x in labels if not all(a<=b for a,b in zip(here,x))]
        labels.append(here)
        self.labels[key]=labels
        return False
