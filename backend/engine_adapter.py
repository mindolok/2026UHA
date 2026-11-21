"""CITY FLOW engine connection (engine/ folder, sequence-based planner)."""
from __future__ import annotations

import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from cityflow.planner import Network, Planner, controls as _controls, stop_check  # noqa: E402

DB = ENGINE_DIR / "cityflow.db"
EVENT_LABEL = "2026-11-01 JTBC 서울마라톤 무정차 공지 기준"

_net = Network(str(DB))
_planner = Planner(_net)
ENGINE_READY = True


def _minute(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def controls(time: str) -> dict:
    return {"event": EVENT_LABEL, **_controls(_net, _minute(time))}


def route(origin: dict, dest: dict, depart: str, subway: bool = True) -> dict:
    res = _planner.search(origin["lat"], origin["lng"], dest["lat"], dest["lng"], _minute(depart),
                          subway=subway)
    return {"event": EVENT_LABEL, **res}


def stop(route_name: str, ars: str, time: str) -> dict:
    return {"event": EVENT_LABEL, **stop_check(_net, route_name.strip(), ars.strip().zfill(5), _minute(time))}
