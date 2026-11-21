"""Candidate evaluation, event-aware sequential checks, and safe branching.

The live transit path generator is intentionally an adapter: the supplied XLSX
files contain stop order/coordinates but NO timetables or actual detour roads.
This module never fabricates bus arrival times, skipped-stop delays, or a
replacement itinerary. Branch candidates must be supplied by a routing source.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappop, heappush
from itertools import count
from typing import Protocol
import json
import sqlite3
from .impact import Impact, check_impact, parse_local_timestamp
from .ingest import normalize_ars

ROLES = frozenset({"BOARD", "VIA", "ALIGHT", "TRANSFER_BOARD", "TRANSFER_ALIGHT"})
BOARD = {"BOARD", "TRANSFER_BOARD"}
ALIGHT = {"ALIGHT", "TRANSFER_ALIGHT"}


@dataclass(frozen=True)
class Visit:
    stop_id: str
    time: str
    route_name: str
    direction: str
    role: str

    @staticmethod
    def from_dict(d: dict) -> "Visit":
        required = ["stop_id", "time", "route_name", "direction", "role"]
        for key in required:
            if key not in d or not str(d[key]).strip():
                raise ValueError(f"Visit requires '{key}'")
        if d["role"] not in ROLES:
            raise ValueError(f"Unexpected visit role: {d['role']}")
        parse_local_timestamp(d["time"])
        stop = normalize_ars(d["stop_id"])
        if not stop:
            raise ValueError("Visit stop_id must be an ARS stop number")
        return Visit(stop, d["time"],d["route_name"],d["direction"],d["role"])


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    root_id: str
    depart_at: str
    arrive_at: str
    walk_distance_m: float
    visits: tuple[Visit, ...]
    parent_id: str | None = None
    pivot_stop_id: str | None = None
    reroute_calls: int = 0
    source: str = "external_path_provider"
    # Adapter-supplied metadata. A routing provider must demonstrate actual
    # board/alight access; XLSX alone cannot prove scheduled bus availability.
    service_evidence: str = "UNVERIFIED"

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        for field_name in ["candidate_id","root_id","depart_at","arrive_at","walk_distance_m","visits"]:
            if field_name not in d:
                raise ValueError(f"Candidate requires '{field_name}'")
        depart = parse_local_timestamp(d["depart_at"])
        arrival = parse_local_timestamp(d["arrive_at"])
        if arrival <= depart:
            raise ValueError("Arrival must follow departure")
        visits = tuple(Visit.from_dict(row) for row in d["visits"])
        if not visits:
            raise ValueError("Candidate needs at least one stop visit")
        times = [parse_local_timestamp(v.time) for v in visits]
        if times != sorted(times) or times[0] < depart or times[-1] > arrival:
            raise ValueError("Stop visit times must be ordered inside the trip")
        walk = float(d["walk_distance_m"])
        if walk < 0:
            raise ValueError("Walking distance cannot be negative")
        calls = int(d.get("reroute_calls",0))
        if calls < 0:
            raise ValueError("reroute_calls cannot be negative")
        return Candidate(str(d["candidate_id"]),str(d["root_id"]),d["depart_at"],
                         d["arrive_at"],walk,visits,d.get("parent_id"),
                         normalize_ars(d.get("pivot_stop_id")) or None,
                         calls,str(d.get("source","external_path_provider")),
                         str(d.get("service_evidence","UNVERIFIED")))

    @property
    def duration_min(self) -> float:
        return (parse_local_timestamp(self.arrive_at)-parse_local_timestamp(self.depart_at)).total_seconds()/60

    @property
    def transfers(self) -> int:
        return max(0,sum(v.role in BOARD for v in self.visits)-1)


@dataclass
class Decision:
    candidate_id: str
    root_id: str
    outcome: str
    duration_min: float
    transfer_count: int
    walk_distance_m: float
    evidence: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"candidate_id":self.candidate_id,"root_id":self.root_id,
                "outcome":self.outcome,"duration_min":round(self.duration_min,2),
                "transfer_count":self.transfer_count,"walk_distance_m":self.walk_distance_m,
                "reasons":self.reasons,"evidence":self.evidence}


class AlternativeProvider(Protocol):
    """Supply complete origin-to-destination candidates after an affected pivot.

    The provider, not this module, must compute service-aware timings and
    actual walk paths. No fabricated bus speed is used inside the engine.
    """
    def reroute(self, candidate: Candidate, affected_stop: Visit,
                pivot_stop_id: str, max_walk_m: float) -> list[Candidate]: ...


class CatalogAlternativeProvider:
    """Offline/test provider. Keys are candidate_id + '|' + pivot_stop_id.

    The catalog is not a live path search; records must come from a validated
    route API, a verified field test, or labeled synthetic test fixtures.
    """
    def __init__(self, alternatives: dict[str,list[dict]] | None = None):
        self.alternatives = alternatives or {}

    def reroute(self,candidate:Candidate,affected_stop:Visit,pivot_stop_id:str,
                max_walk_m:float) -> list[Candidate]:
        key = f"{candidate.candidate_id}|{pivot_stop_id}"
        result=[]
        for item in self.alternatives.get(key,[]):
            child = Candidate.from_dict(item)
            if child.parent_id != candidate.candidate_id or child.root_id != candidate.root_id:
                raise ValueError(f"Catalog branch {child.candidate_id} must preserve root and parent")
            if child.pivot_stop_id != pivot_stop_id:
                raise ValueError(f"Catalog branch {child.candidate_id} has incorrect pivot")
            if child.depart_at != candidate.depart_at:
                raise ValueError("Branch must preserve original departure for total-time comparison")
            if child.reroute_calls != candidate.reroute_calls+1:
                raise ValueError("reroute_calls must increment exactly once")
            result.append(child)
        return result


def stop_on_route(db:sqlite3.Connection,route_name:str,stop_id:str) -> bool:
    return bool(db.execute("""SELECT EXISTS(SELECT 1 FROM route_stops
                         WHERE route_name=? AND ars_id=?)""",
                         (route_name,stop_id)).fetchone()[0])


def _pivot_from_verified_context(candidate:Candidate, affected_index:int) -> str | None:
    visit=candidate.visits[affected_index]
    if visit.role in BOARD:
        # The passenger is physically at this stop, but the bus does not serve it.
        return visit.stop_id
    # An ALIGHT failure cannot simply start walking from a stop where the bus
    # does not stop. Supply the last truly reachable stop from a verified detour.
    # For this candidate schema, the adapter may give a validated pivot_stop_id.
    return candidate.pivot_stop_id


def _pareto_front(decisions: list[Decision]) -> list[Decision]:
    """Minimize duration, transfer count, and walking distance."""
    result=[]
    for d in decisions:
        dominated=False
        for other in decisions:
            if other is d:
                continue
            a=(other.duration_min,other.transfer_count,other.walk_distance_m)
            b=(d.duration_min,d.transfer_count,d.walk_distance_m)
            if all(x<=y for x,y in zip(a,b)) and any(x<y for x,y in zip(a,b)):
                dominated=True;break
        if not dominated:
            result.append(d)
    return sorted(result,key=lambda d:(d.duration_min,d.transfer_count,d.walk_distance_m,d.candidate_id))


def evaluate_candidates(db:sqlite3.Connection,
                        baselines:list[Candidate],
                        provider:AlternativeProvider,
                        baseline_limit_mode:str,
                        max_extra_minutes:float=60,
                        max_transfers:int=5,
                        max_reroute_calls:int=2,
                        max_walk_m:float=300,
                        top_k:int=3) -> dict:
    """Evaluate route branches without assuming missing transit/walk/delay data.

    baseline_limit_mode is REQUIRED because PDF v2 explicitly leaves global
    fastest vs. per-root baseline for the team to decide.
    """
    if baseline_limit_mode not in {"global_fastest","per_root"}:
        raise ValueError("TEAM DECISION REQUIRED: baseline_limit_mode must be global_fastest or per_root")
    if not baselines:
        raise ValueError("At least one baseline candidate is required from a path provider")
    if any(c.parent_id is not None for c in baselines):
        raise ValueError("Baseline candidate must have parent_id=null")
    roots={c.root_id:c.duration_min for c in baselines}
    if len(roots)!=len(baselines):
        raise ValueError("Root identifiers must be unique")
    global_t0=min(roots.values())
    queue=[];serial=count();visited_ids=set();decisions=[]
    def push(candidate:Candidate):
        if candidate.candidate_id in visited_ids:
            return
        visited_ids.add(candidate.candidate_id)
        heappush(queue,(candidate.duration_min,next(serial),candidate))
    for c in baselines:push(c)
    while queue:
        _,__,candidate=heappop(queue)
        dec=Decision(candidate.candidate_id,candidate.root_id,"PENDING",
                     candidate.duration_min,candidate.transfers,candidate.walk_distance_m)
        t0=global_t0 if baseline_limit_mode=="global_fastest" else roots[candidate.root_id]
        if candidate.duration_min>t0+max_extra_minutes:
            dec.outcome="PRUNED_DURATION";dec.reasons.append("Total duration exceeds reference + 60 min")
            decisions.append(dec);continue
        if candidate.transfers>max_transfers:
            dec.outcome="PRUNED_TRANSFERS";dec.reasons.append("Bus transfer count exceeds 5")
            decisions.append(dec);continue
        terminal=None
        for i,visit in enumerate(candidate.visits):
            if not stop_on_route(db,visit.route_name,visit.stop_id):
                dec.outcome="REVIEW_REQUIRED"
                dec.reasons.append("Input route/stop combination is absent from dated master")
                terminal="review";break
            status=check_impact(db,visit.route_name,visit.stop_id,visit.time,visit.direction)
            if status.status in {"PLANNED_SKIP","POSSIBLE_SKIP_REVIEW"}:
                dec.evidence.extend(status.evidence)
                if visit.role=="VIA":
                    # Pass-through stop skip does not prohibit staying onboard.
                    # However downstream times are unknown without real detour data.
                    dec.reasons.append("Mid-route stop skip: bus may continue, but travel time/rejoin unverified")
                    dec.outcome="REVIEW_REQUIRED"
                    terminal="review";break
                if status.status=="POSSIBLE_SKIP_REVIEW":
                    dec.reasons.append("Potential skipped boarding/alighting stop; original notice needs review")
                pivot=_pivot_from_verified_context(candidate,i)
                if pivot is None:
                    dec.outcome="NO_VERIFIED_PIVOT"
                    dec.reasons.append("Cannot reroute from an unserved ALIGHT stop without a verified reachable pivot")
                    terminal="blocked";break
                if candidate.reroute_calls>=max_reroute_calls:
                    dec.outcome="BRANCH_CALL_LIMIT"
                    dec.reasons.append("Maximum reroute depth reached")
                    terminal="blocked";break
                children=provider.reroute(candidate,visit,pivot,max_walk_m)
                if not children:
                    dec.outcome="NEEDS_REROUTE_PROVIDER"
                    dec.reasons.append(f"No validated new itinerary provided from pivot {pivot}")
                    terminal="blocked";break
                for child in children:
                    push(child)
                dec.outcome="BRANCHED"
                dec.reasons.append(f"Generated {len(children)} candidate(s) from pivot {pivot}")
                terminal="branched";break
            if status.status in {"DIRECTION_REQUIRED","INVALID_STOP_ID"}:
                dec.outcome="REVIEW_REQUIRED";dec.reasons.append(status.status)
                terminal="review";break
        if terminal is None:
            if candidate.service_evidence != "VERIFIED":
                dec.outcome="REVIEW_REQUIRED"
                dec.reasons.append("Routing provider has not verified bus service and walk connections")
            else:
                dec.outcome="NOTICE_COMPATIBLE"
                dec.reasons.append("No conflicting planned skip found for checked visits")
                dec.reasons.append("Not proof of historical real-time operation")
        decisions.append(dec)
    compatible=[d for d in decisions if d.outcome=="NOTICE_COMPATIBLE"]
    # Pareto first, then fastest eligible candidates.
    ranked=_pareto_front(compatible)[:top_k]
    return {
        "mode":baseline_limit_mode,"reference_fastest_min":global_t0,
        "max_extra_minutes":max_extra_minutes,"max_transfers":max_transfers,
        "created_candidates":len(visited_ids),
        "ranked":[d.as_dict() for d in ranked],
        "decisions":[d.as_dict() for d in decisions]
    }


def evaluate_catalog_file(db:sqlite3.Connection,path:str,mode:str) -> dict:
    with open(path,encoding="utf-8") as f: data=json.load(f)
    roots=[Candidate.from_dict(row) for row in data["baselines"]]
    return evaluate_candidates(db,roots,CatalogAlternativeProvider(data.get("alternatives",{})),mode)
