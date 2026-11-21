"""CITY FLOW engine connection (engine/ folder, sequence-based planner)."""
from __future__ import annotations

import os
import sys
import threading
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


# 약한 서버(무료 호스팅)에서는 "fast"(기본), 여유 있는 PC에서는 CITYFLOW_EFFORT=full
EFFORT = os.getenv("CITYFLOW_EFFORT", "fast")
_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


def _key(origin, dest, depart, subway):
    r = lambda v: round(float(v), 4)          # ~10 m
    return (r(origin["lat"]), r(origin["lng"]), r(dest["lat"]), r(dest["lng"]), depart, bool(subway))


def route(origin: dict, dest: dict, depart: str, subway: bool = True) -> dict:
    k = _key(origin, dest, depart, subway)
    if k in _cache:
        return _cache[k]
    with _lock:                               # one search at a time on a 0.1-CPU server
        if k in _cache:
            return _cache[k]
        res = _planner.search(origin["lat"], origin["lng"], dest["lat"], dest["lng"], _minute(depart),
                              subway=subway, effort=EFFORT)
        out = {"event": EVENT_LABEL, **res}
        if len(_cache) > 300:
            _cache.clear()
        _cache[k] = out
        return out


# 시연 예시(frontend/app.js EXAMPLES와 같은 좌표·시각)는 서버가 켜질 때 미리 계산
DEMOS = [
    ({"lat": 37.5259231137, "lng": 126.9243582037}, {"lat": 37.576162, "lng": 126.971415}, "08:00"),
    ({"lat": 37.573648, "lng": 126.976997}, {"lat": 37.535233709, "lng": 127.0689474032}, "10:00"),
    ({"lat": 37.5502676468, "lng": 127.0077186988}, {"lat": 37.485411, "lng": 127.03885}, "09:00"),
]


def _warm():
    for o, d, t in DEMOS:
        for sub in (True, False):
            try:
                route(o, d, t, sub)
            except Exception:                 # noqa: BLE001 - warming must never stop the server
                pass


threading.Thread(target=_warm, daemon=True).start()


def stop(route_name: str, ars: str, time: str) -> dict:
    return {"event": EVENT_LABEL, **stop_check(_net, route_name.strip(), ars.strip().zfill(5), _minute(time))}
