"""Sequence-based control checks and event-aware route search (final engine).

Team decisions applied (2026-11-21):
  * Event rows are matched on (route_name, stop_sequence), which matches the
    route master for 2,720/2,729 rows. Direction text is display-only.
  * T0* = fastest original (no-event) path, fixed; candidates > T0* + 60 min
    or > 5 transfers are pruned (equality allowed).
  * Passing through a skipped block adds an ESTIMATED +10 min, once per block.
  * Rows with review_state != '확인' are treated as controlled. A candidate that
    rides through such a stop goes to `needs_check`, never to `confirmed`.

The route master has no timetables, so travel times are ESTIMATES (see
`Assumptions`). Every result carries `estimated=True` and the assumptions used.
A real provider (e.g. Kakao transit) can replace `Planner.search` later.
"""
from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path
from collections import defaultdict
from dataclasses import asdict, dataclass, field


# --------------------------------------------------------------------------- #
# assumptions (all shown as "추정" in the UI)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Assumptions:
    bus_kmh: float = 17.0          # average bus speed
    detour_factor: float = 1.3     # road distance / straight line
    dwell_s: float = 20.0          # per-stop dwell
    wait_s: float = 360.0          # waiting time per boarding
    walk_mps: float = 1.2          # walking speed
    walk_detour: float = 1.2       # walking path / straight line
    transfer_walk_m: float = 300.0
    access_walk_m: float = 400.0   # origin/destination radius (G)
    detour_delay_s: float = 600.0  # +10 min once per skipped block
    unbounded_end_pad_min: int = 20  # '통제해제시' = max end + 20 min
    subway_wait_s: float = 240.0   # headway/2 + platform access
    subway_egress_s: float = 120.0 # platform -> street after alighting
    subway_dwell_s: float = 30.0
    subway_open_min: int = 5 * 60 + 30
    subway_close_min: int = 24 * 60
    interchange_max_m: float = 600.0   # same-name stations of different lines
    max_extra_min: float = 60.0
    max_transfers: int = 5
    night_start_min: int = 23 * 60
    night_end_min: int = 5 * 60 + 30
    dawn_start_min: int = 3 * 60 + 30  # '새벽' buses, estimated window
    dawn_end_min: int = 7 * 60


A = Assumptions()


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.asin(min(1.0, math.sqrt(a)))


def hhmm_to_min(text: str) -> int | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def min_to_hhmm(m: float) -> str:
    m = int(round(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #
@dataclass
class Visit:
    seq: int
    ars: str
    name: str
    lat: float
    lng: float
    virtual: bool


@dataclass
class Window:
    start: int
    end: int
    confirmed: bool          # review_state == '확인'
    end_estimated: bool      # '통제해제시' etc.
    block: int               # skipped-block id (gid) within the route
    direction: str
    stop_name: str
    pdf_page: str
    event_id: int

    def active(self, minute: float) -> bool:
        return self.start <= minute <= self.end


@dataclass
class Status:
    """Impact of events on one directed stop visit (route, seq) at a time."""
    state: str               # NORMAL | CONTROLLED | CHECK | VIRTUAL
    windows: list[Window] = field(default_factory=list)

    @property
    def blocked(self) -> bool:  # cannot board / alight here
        return self.state != "NORMAL"


class Network:
    def __init__(self, db_path: str, a: Assumptions = A, subway_dir: str | None = None):
        self.a = a
        sd = Path(subway_dir) if subway_dir else Path(db_path).resolve().parent / "data" / "subway"
        self.subway_dir = sd if (sd / "subway_times.csv").exists() else None
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            self._load(con)
        finally:
            con.close()

    # -- loading ---------------------------------------------------------- #
    def _load(self, con: sqlite3.Connection) -> None:
        a = self.a
        self.routes: dict[str, str] = {}             # route_id -> name
        self.route_by_name: dict[str, str] = {}
        self.visits: dict[str, list[Visit]] = defaultdict(list)
        for rid, name, seq, ars, sname, lng, lat in con.execute(
                "SELECT route_id,route_name,seq,ars_id,name,lng,lat FROM route_stops ORDER BY route_id,seq"):
            self.routes[rid] = name
            self.route_by_name[name] = rid
            self.visits[rid].append(Visit(seq, ars, sname, lat, lng, "(가상)" in sname))
        self.kind: dict[str, str] = {rid: "bus" for rid in self.routes}
        self.cum: dict[str, list[float]] = {}
        self.subway_skipped: list[str] = []
        if self.subway_dir:
            self._load_subway()
        self.night = {rid for rid, n in self.routes.items()
                      if self.kind[rid] == "bus" and (re.match(r"^N\d", n) or "심야" in n)}
        self.dawn = {rid for rid, n in self.routes.items() if "새벽" in n}

        # stops keyed by ARS (coordinates from the route file, per design doc)
        self.stops: dict[str, tuple[str, float, float]] = {}
        self.stop_routes: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for rid, vs in self.visits.items():
            if rid in self.cum:                      # subway: cum already from timetable
                for i, v in enumerate(vs):
                    self.stops.setdefault(v.ars, (v.name, v.lat, v.lng))
                    self.stop_routes[v.ars].append((rid, i))
                continue
            c = [0.0]
            for i in range(1, len(vs)):
                d = haversine_m(vs[i - 1].lat, vs[i - 1].lng, vs[i].lat, vs[i].lng)
                c.append(c[-1] + d * a.detour_factor / (a.bus_kmh / 3.6)
                         + (0 if vs[i].virtual else a.dwell_s))
            self.cum[rid] = c
            for i, v in enumerate(vs):
                if not v.virtual:
                    self.stops.setdefault(v.ars, (v.name, v.lat, v.lng))
                    self.stop_routes[v.ars].append((rid, i))

        # events -> windows on (route_id, seq)
        rows = con.execute("""SELECT event_id,route_name,direction,start_text,end_text,
            skipped_stop_name,ars_id,stop_sequence,review_state,pdf_page,notes
            FROM events""").fetchall()
        ends = [hhmm_to_min(r[4]) for r in rows]
        unbounded_end = max((e for e in ends if e is not None), default=23 * 60 + 39) + a.unbounded_end_pad_min
        self.unbounded_end = unbounded_end
        self.windows: dict[tuple[str, int], list[Window]] = defaultdict(list)
        self.route_notes: dict[str, list[dict]] = defaultdict(list)  # rows without a stop
        # rows with a stop ID but no sequence: direction uncertain -> NOT used for
        # decisions, only shown as a caution on the citizen screen
        self.uncertain: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.unmatched: list[int] = []
        per_route: dict[str, list[tuple]] = defaultdict(list)
        for (eid, rname, direc, st, en, sname, ars, sseq, review, page, notes) in rows:
            start = hhmm_to_min(st) or 0
            end = hhmm_to_min(en)
            rid = self.route_by_name.get(rname)
            try:
                seq = int(float(sseq))
            except (TypeError, ValueError):
                seq = None
            if seq is None and ars:
                self.uncertain[(rname, ars)].append({
                    "direction": direc, "start": start,
                    "end": end if end is not None else unbounded_end,
                    "end_estimated": end is None, "pdf_page": page, "stop_name": sname})
                continue
            if rid is None or seq is None:
                self.route_notes[rname].append({
                    "event_id": eid, "direction": direc, "start": start,
                    "end": end if end is not None else unbounded_end,
                    "note": notes, "pdf_page": page})
                continue
            per_route[rid].append((seq, eid, direc, start, end, sname, ars, review, page))

        for rid, items in per_route.items():
            seq_index = {v.seq: i for i, v in enumerate(self.visits[rid])}
            items.sort()
            block, last_idx = -1, None
            for (seq, eid, direc, start, end, sname, ars, review, page) in items:
                idx = seq_index.get(seq)
                if idx is None or self.visits[rid][idx].ars != ars:
                    self.unmatched.append(eid)
                    continue
                # same block if every visit between the two rows is virtual
                if last_idx is None or not all(self.visits[rid][j].virtual
                                               for j in range(last_idx + 1, idx)):
                    if last_idx != idx:
                        block += 1
                last_idx = idx
                self.windows[(rid, seq)].append(Window(
                    start, end if end is not None else unbounded_end,
                    review == "확인", end is None, block, direc, sname, page, eid))

        # walking transfer graph (grid hash, straight line * walk_detour)
        cell = 0.004
        grid: dict[tuple[int, int], list[str]] = defaultdict(list)
        for ars, (_, lat, lng) in self.stops.items():
            grid[(int(lat / cell), int(lng / cell))].append(ars)
        self.grid, self.cell = grid, cell
        self.walk: dict[str, list[tuple[str, float]]] = {}
        for ars, (_, lat, lng) in self.stops.items():
            self.walk[ars] = [(o, d) for o, d in self.near(lat, lng, a.transfer_walk_m) if o != ars]
        # subway interchanges: same station name on different lines
        by_name: dict[str, list[str]] = defaultdict(list)
        for sid in self.stops:
            if sid.startswith("S") and ":" in sid:
                by_name[sid.split(":", 1)[1]].append(sid)
        for ids in by_name.values():
            for x in ids:
                for y in ids:
                    if x == y:
                        continue
                    d = max(150.0, haversine_m(*self.stops[x][1:], *self.stops[y][1:]) * a.walk_detour)
                    if d <= a.interchange_max_m and all(o != y for o, _ in self.walk[x]):
                        self.walk[x].append((y, d))

    def _load_subway(self) -> None:
        from .subway import load_lines
        data = load_lines(self.subway_dir)
        self.subway_skipped = data["skipped"]
        for rid, line, label, seq in data["routes"]:
            self.routes[rid] = f"{line}호선"
            self.kind[rid] = "subway"
            self.direction_label = getattr(self, "direction_label", {})
            self.direction_label[rid] = label
            vs, c = [], [0.0]
            for i, (name, lat, lng, run) in enumerate(seq):
                vs.append(Visit(i + 1, f"S{line}:{name}", f"{name}역", lat, lng, False))
                if i:
                    c.append(c[-1] + run + self.a.subway_dwell_s)
            self.visits[rid] = vs
            self.cum[rid] = c

    def wait_s(self, rid: str) -> float:
        return self.a.subway_wait_s if self.kind.get(rid) == "subway" else self.a.wait_s

    def egress_min(self, rid: str) -> float:
        return self.a.subway_egress_s / 60 if self.kind.get(rid) == "subway" else 0.0

    # -- queries ---------------------------------------------------------- #
    def near(self, lat: float, lng: float, radius_m: float) -> list[tuple[str, float]]:
        r = int(radius_m / 111000 / self.cell) + 1
        ci, cj = int(lat / self.cell), int(lng / self.cell)
        out = []
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for ars in self.grid.get((i, j), ()):
                    _, la, ln = self.stops[ars]
                    d = haversine_m(lat, lng, la, ln) * self.a.walk_detour
                    if d <= radius_m:
                        out.append((ars, d))
        out.sort(key=lambda x: x[1])
        return out

    def status(self, rid: str, idx: int, minute: float) -> Status:
        v = self.visits[rid][idx]
        if v.virtual:
            return Status("VIRTUAL")
        act = [w for w in self.windows.get((rid, v.seq), ()) if w.active(minute % 1440)]
        if not act:
            return Status("NORMAL")
        return Status("CONTROLLED" if all(w.confirmed for w in act) else "CHECK", act)

    def route_warnings(self, route_name: str, minute: float) -> list[dict]:
        """Notices that name no stop (e.g. '무정차 정류소 기재 없음')."""
        return [n for n in self.route_notes.get(route_name, ())
                if n["start"] <= minute % 1440 <= n["end"]]

    def runs(self, rid: str, minute: float) -> bool:
        m = minute % 1440
        if self.kind.get(rid) == "subway":
            return self.a.subway_open_min <= m <= self.a.subway_close_min
        if rid in self.night:
            return m >= self.a.night_start_min or m <= self.a.night_end_min
        if rid in self.dawn:
            return self.a.dawn_start_min <= m <= self.a.dawn_end_min
        return True

    def pivots(self, rid: str, idx: int, minute: float) -> dict:
        """Last servable stop before, and first servable stop after, a skipped visit
        on the same directed route (the two alighting branches, PDF R1d)."""
        vs = self.visits[rid]
        before = after = None
        for j in range(idx - 1, -1, -1):
            if not self.status(rid, j, minute).blocked:
                before = j
                break
        for j in range(idx + 1, len(vs)):
            t = minute + (self.cum[rid][j] - self.cum[rid][idx]) / 60
            if not self.status(rid, j, t).blocked:
                after = j
                break
        return {"before": before, "after": after}


# --------------------------------------------------------------------------- #
# citizen screen: "does this bus stop here?"
# --------------------------------------------------------------------------- #
def stop_check(net: Network, route_name: str, ars: str, minute: float) -> dict:
    rid = net.route_by_name.get(route_name)
    if rid is None:
        return {"status": "UNKNOWN_ROUTE", "message": f"{route_name}번 노선을 찾을 수 없습니다."}
    idxs = [i for i, v in enumerate(net.visits[rid]) if v.ars == ars]
    if not idxs:
        return {"status": "NOT_ON_ROUTE", "message": f"{route_name}번은 {ars} 정류소를 지나지 않습니다."}
    out = []
    for idx in idxs:
        v = net.visits[rid][idx]
        st = net.status(rid, idx, minute)
        nxt = next((net.visits[rid][j].name for j in range(idx + 1, len(net.visits[rid]))
                    if not net.visits[rid][j].virtual), "종점")
        item = {"seq": v.seq, "name": v.name, "toward": nxt, "state": st.state,
                "lat": v.lat, "lng": v.lng,
                "windows": [{"start": min_to_hhmm(w.start), "end": min_to_hhmm(w.end),
                             "end_estimated": w.end_estimated, "direction": w.direction,
                             "pdf_page": w.pdf_page, "confirmed": w.confirmed} for w in st.windows],
                "alternatives": []}
        if st.blocked:
            p = net.pivots(rid, idx, minute)
            for key, label in (("before", "이전 정류장"), ("after", "다음 운행 정류장")):
                j = p[key]
                if j is None:
                    continue
                a = net.visits[rid][j]
                d = haversine_m(v.lat, v.lng, a.lat, a.lng) * net.a.walk_detour
                item["alternatives"].append({
                    "kind": label, "seq": a.seq, "ars": a.ars, "name": a.name,
                    "lat": a.lat, "lng": a.lng, "walk_m": round(d),
                    "walk_min": round(d / net.a.walk_mps / 60, 1)})
        out.append(item)
    worst = "CONTROLLED" if any(o["state"] == "CONTROLLED" for o in out) else \
            "CHECK" if any(o["state"] == "CHECK" for o in out) else "NORMAL"
    msg = {"NORMAL": "이 시각에는 무정차 공지가 없습니다.",
           "CONTROLLED": "이 시각에는 이 정류장에 서지 않습니다.",
           "CHECK": "무정차 가능성이 있습니다(공지 확인 필요). 정차하지 않는다고 보고 안내합니다."}[worst]
    m = minute % 1440
    unsure = [{"direction": u["direction"], "start": min_to_hhmm(u["start"]), "end": min_to_hhmm(u["end"]),
               "end_estimated": u["end_estimated"], "pdf_page": u["pdf_page"]}
              for u in net.uncertain.get((route_name, ars), ()) if u["start"] <= m <= u["end"]]
    return {"status": worst, "message": msg, "route": route_name, "ars": ars,
            "visits": out, "route_warnings": net.route_warnings(route_name, minute),
            "uncertain_notices": unsure}


# --------------------------------------------------------------------------- #
# route search (RAPTOR-style, frequency-based, event-aware)
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    mode: str                 # walk | bus
    from_name: str
    to_name: str
    depart_min: float
    arrive_min: float
    path: list[list[float]]
    route: str | None = None
    board_seq: int | None = None
    alight_seq: int | None = None
    distance_m: float = 0.0
    passed_blocks: list[dict] = field(default_factory=list)
    rid: str | None = None
    kind: str = "walk"


@dataclass
class Plan:
    depart_min: float
    arrive_min: float
    transfers: int
    walk_m: float
    legs: list[Leg]
    needs_check: bool
    reasons: list[str]

    @property
    def minutes(self) -> float:
        return self.arrive_min - self.depart_min

    def to_dict(self) -> dict:
        buses = [l.route for l in self.legs if l.mode == "bus"]
        return {"summary": " → ".join(buses) if buses else "도보",
                "minutes": round(self.minutes), "transfers": self.transfers,
                "walk_m": round(self.walk_m), "depart": min_to_hhmm(self.depart_min),
                "arrive": min_to_hhmm(self.arrive_min), "needs_check": self.needs_check,
                "reasons": self.reasons, "estimated": True,
                "legs": [{**asdict(l), "depart": min_to_hhmm(l.depart_min),
                          "arrive": min_to_hhmm(l.arrive_min),
                          "label": (f"{l.route}번 {l.from_name} → {l.to_name}" if l.mode == "bus"
                                    else f"도보 {round(l.distance_m)}m")} for l in self.legs]}


class Planner:
    def __init__(self, net: Network):
        self.net = net

    # mode: "baseline" (ignore events), "strict" (CHECK = forbidden),
    #       "relaxed" (CHECK stops may be ridden through, flagged)
    def _raptor(self, olat, olng, dlat, dlng, t0: float, mode: str,
                max_rounds: int, budget: float | None, banned: frozenset = frozenset()):
        net, a = self.net, self.net.a
        ws = a.walk_mps
        best: dict[str, float] = {}
        labels: list[dict[str, tuple]] = [{}]
        for ars, d in net.near(olat, olng, a.access_walk_m):
            t = t0 + d / ws / 60
            labels[0][ars] = (t, ("origin", d))
            best[ars] = t
        goals = dict(net.near(dlat, dlng, a.access_walk_m))
        if mode != "baseline":
            # PDF branches: boarding stop skipped -> walk to the same route's
            # previous/next servable stop; alighting stop skipped -> ride to the
            # last servable stop before, or the rejoin stop after, then walk.
            for ars, d in list(net.near(olat, olng, a.access_walk_m)):
                for rid, i in net.stop_routes.get(ars, ()):
                    if net.status(rid, i, t0 + d / ws / 60).blocked:
                        for j in net.pivots(rid, i, t0).values():
                            if j is None:
                                continue
                            v = net.visits[rid][j]
                            dd = d + haversine_m(*net.stops[ars][1:], v.lat, v.lng) * a.walk_detour
                            t = t0 + dd / ws / 60
                            if t < best.get(v.ars, math.inf):
                                labels[0][v.ars] = (t, ("origin", dd))
                                best[v.ars] = t
            for ars, d in list(goals.items()):
                for rid, i in net.stop_routes.get(ars, ()):
                    # time unknown before search; use departure (windows are hours long)
                    if net.status(rid, i, t0).blocked:
                        for j in net.pivots(rid, i, t0).values():
                            if j is None:
                                continue
                            v = net.visits[rid][j]
                            dd = d + haversine_m(*net.stops[ars][1:], v.lat, v.lng) * a.walk_detour
                            if dd < goals.get(v.ars, math.inf):
                                goals[v.ars] = dd
        marked = set(labels[0])
        for k in range(1, max_rounds + 1):
            prev_best = dict(best)
            lab: dict[str, tuple] = {}
            routes: dict[str, int] = {}
            for s in marked:
                for rid, i in net.stop_routes.get(s, ()):
                    if rid not in banned and i < routes.get(rid, 1 << 30):
                        routes[rid] = i
            improved = set()
            for rid, start in routes.items():
                vs, cum = net.visits[rid], net.cum[rid]
                rname = net.routes[rid]
                wait_min, egress = net.wait_s(rid) / 60, net.egress_min(rid)
                board = None   # (idx, board_time, delay_s, blocks_seen, flags)
                for i in range(start, len(vs)):
                    v = vs[i]
                    if board is not None:
                        bi, bt, delay, seen, flags = board
                        t = bt + (cum[i] - cum[bi] + delay) / 60
                        if mode == "baseline":
                            st = Status("VIRTUAL" if v.virtual else "NORMAL")
                        else:
                            st = net.status(rid, i, t)
                        if st.state in ("CONTROLLED", "CHECK"):
                            if st.state == "CHECK" and mode == "strict":
                                board = None          # strict: may not ride through
                            else:
                                blk = st.windows[0].block
                                if blk not in seen:
                                    seen = seen | {blk}
                                    delay += a.detour_delay_s
                                    t += a.detour_delay_s / 60
                                if st.state == "CHECK":
                                    flags = flags | {"check"}
                                board = (bi, bt, delay, seen, flags)
                        elif st.state == "NORMAL":
                            if budget is not None and t > budget:
                                board = None
                            else:
                                ta = t + egress
                                if ta < best.get(v.ars, math.inf):
                                    best[v.ars] = ta
                                    lab[v.ars] = (ta, ("bus", rid, bi, i, bt, delay, seen, flags))
                                    improved.add(v.ars)
                    # (re)board here?
                    if v.virtual or v.ars not in prev_best or not net.runs(rid, prev_best[v.ars]):
                        continue
                    tb = prev_best[v.ars] + wait_min
                    cur = None if board is None else board[1] + (cum[i] - cum[board[0]] + board[2]) / 60
                    if cur is not None and tb >= cur:
                        continue
                    if mode != "baseline":
                        if net.status(rid, i, tb).blocked:
                            continue
                        warn = net.route_warnings(rname, tb)
                        if warn and mode == "strict":
                            continue
                        flags = frozenset({"route_note"}) if warn else frozenset()
                    else:
                        flags = frozenset()
                    board = (i, tb, 0.0, frozenset(), flags)
            # walking transfers from stops reached by bus in this round
            for s in list(improved):
                ts = lab[s][0]
                for o, d in net.walk.get(s, ()):
                    t = ts + d / ws / 60
                    if t < best.get(o, math.inf):
                        best[o] = t
                        lab[o] = (t, ("walk", s, d))
                        improved.add(o)
            labels.append(lab)
            marked = improved
            if not marked:
                break
        return labels, goals

    def _reconstruct(self, labels, k, stop, olat, olng):
        net = self.net
        legs: list[Leg] = []
        reasons: set[str] = set()
        def find(k, s):
            while k >= 0:
                if s in labels[k]:
                    return k, labels[k][s]
                k -= 1
            raise KeyError(s)
        k, (t, par) = find(k, stop)
        while True:
            kind = par[0]
            if kind == "origin":
                n, la, ln = net.stops[stop]
                legs.append(Leg("walk", "출발지", n, t - par[1] / net.a.walk_mps / 60, t,
                                [[olat, olng], [la, ln]], distance_m=par[1]))
                break
            if kind == "walk":
                _, s, d = par
                n1, la1, ln1 = net.stops[s]
                n2, la2, ln2 = net.stops[stop]
                legs.append(Leg("walk", n1, n2, t - d / net.a.walk_mps / 60, t,
                                [[la1, ln1], [la2, ln2]], distance_m=d))
                stop = s
                k, (t, par) = find(k, s)
                continue
            _, rid, bi, ai, bt, delay, seen, flags = par
            vs = net.visits[rid]
            if "check" in flags:
                reasons.add("통제 '확인 필요' 구간을 지나갑니다")
            if "route_note" in flags:
                reasons.add(f"{net.routes[rid]}번: 정류소가 특정되지 않은 통제 공지가 있습니다")
            blocks = []
            for b in sorted(seen):
                w = next(w for (r, _), ws_ in net.windows.items() if r == rid
                         for w in ws_ if w.block == b)
                blocks.append({"block": b, "stop_name": w.stop_name, "pdf_page": w.pdf_page,
                               "confirmed": w.confirmed, "delay_min_estimated": net.a.detour_delay_s / 60})
            legs.append(Leg("bus", vs[bi].name, vs[ai].name, bt, t,
                            [[v.lat, v.lng] for v in vs[bi:ai + 1]], route=net.routes[rid],
                            board_seq=vs[bi].seq, alight_seq=vs[ai].seq, passed_blocks=blocks, rid=rid,
                            kind=net.kind[rid]))
            stop = vs[bi].ars
            k, (t, par) = find(k - 1, stop)
        legs.reverse()
        return legs, reasons

    def _plans(self, olat, olng, dlat, dlng, t0, mode, budget, banned=frozenset()):
        net, a = self.net, self.net.a
        labels, goals = self._raptor(olat, olng, dlat, dlng, t0, mode, a.max_transfers + 1, budget, banned)
        plans, best_arr = [], math.inf
        direct = haversine_m(olat, olng, dlat, dlng) * a.walk_detour
        if direct <= 1500:
            arr = t0 + direct / a.walk_mps / 60
            plans.append(Plan(t0, arr, 0, direct,
                              [Leg("walk", "출발지", "도착지", t0, arr, [[olat, olng], [dlat, dlng]],
                                   distance_m=direct)], False, []))
            best_arr = arr
        for k in range(1, len(labels)):
            cand = None
            for g, d in goals.items():
                for kk in range(k, -1, -1):
                    if g in labels[kk]:
                        if kk == k:
                            arr = labels[k][g][0] + d / a.walk_mps / 60
                            if cand is None or arr < cand[0]:
                                cand = (arr, g, d)
                        break
            if cand is None or cand[0] >= best_arr - 0.5:
                continue
            arr, g, d = cand
            legs, reasons = self._reconstruct(labels, k, g, olat, olng)
            n, la, ln = net.stops[g]
            legs.append(Leg("walk", n, "도착지", arr - d / a.walk_mps / 60, arr,
                            [[la, ln], [dlat, dlng]], distance_m=d))
            nb = sum(l.mode == "bus" for l in legs)
            walk = sum(l.distance_m for l in legs if l.mode == "walk")
            plans.append(Plan(t0, arr, max(0, nb - 1), walk, legs, bool(reasons), sorted(reasons)))
            best_arr = arr
        return plans

    # ------------------------------------------------------------------ #
    # candidate generation: same search, re-run with used routes removed  #
    # ------------------------------------------------------------------ #
    def _diverse(self, o, d, t0, mode, budget, max_runs, limit=None) -> list[Plan]:
        found: dict[tuple, Plan] = {}
        queue, tried, runs = [getattr(self, "_ban0", frozenset())], set(), 0
        while queue and runs < max_runs:
            ban = queue.pop(0)
            if ban in tried:
                continue
            tried.add(ban)
            runs += 1
            plans = self._plans(*o, *d, t0, mode, budget, ban)
            for p in plans:
                if limit is not None and p.minutes > limit:
                    continue
                sig = tuple(l.route for l in p.legs if l.mode == "bus")
                if sig not in found or p.minutes < found[sig].minutes:
                    found[sig] = p
            for p in sorted(plans, key=lambda p: p.minutes)[:2]:
                for r in dict.fromkeys(l.rid for l in p.legs if l.mode == "bus"):
                    nb = ban | {r}
                    if nb not in tried:
                        queue.append(nb)
        return list(found.values())

    # ------------------------------------------------------------------ #
    # PDF step: sequential, role-based check of an ORIGINAL route         #
    # ------------------------------------------------------------------ #
    def check_original(self, plan: Plan) -> dict:
        net, a = self.net, self.net.a
        buses = [l for l in plan.legs if l.mode == "bus"]
        impacts, delay, usable, check, seen = [], 0.0, True, False, set()
        for n, leg in enumerate(buses):
            rid = leg.rid
            vs, cum = net.visits[rid], net.cum[rid]
            idx = {v.seq: i for i, v in enumerate(vs)}
            bi, ai = idx[leg.board_seq], idx[leg.alight_seq]
            for j in range(bi, ai + 1):
                v = vs[j]
                if v.virtual:
                    continue
                t = leg.depart_min + delay + (cum[j] - cum[bi]) / 60
                st = net.status(rid, j, t)
                if not st.blocked:
                    continue
                role = ("BOARD" if n == 0 else "TRANSFER_BOARD") if j == bi else \
                       ("ALIGHT" if n == len(buses) - 1 else "TRANSFER_ALIGHT") if j == ai else "VIA"
                w = st.windows[0]
                item = {"route": leg.route, "stop": v.name, "ars": v.ars, "seq": v.seq,
                        "time": min_to_hhmm(t), "role": role, "state": st.state,
                        "until": min_to_hhmm(max(x.end for x in st.windows)), "pdf_page": w.pdf_page}
                if st.state == "CHECK":
                    check = True
                if role == "VIA":
                    key = (rid, w.block)
                    if key in seen:
                        continue
                    seen.add(key)
                    delay += a.detour_delay_s / 60
                    item["effect"] = "무정차 구간 통과 · 우회 지연 약 +10분(추정)"
                else:
                    usable = False
                    item["effect"] = {"BOARD": "타는 정류장에 버스가 서지 않음",
                                      "TRANSFER_BOARD": "갈아타는 정류장에 버스가 서지 않음",
                                      "ALIGHT": "내리는 정류장에 버스가 서지 않음",
                                      "TRANSFER_ALIGHT": "갈아타려고 내리는 정류장에 버스가 서지 않음"}[role]
                impacts.append(item)
        status = "BLOCKED" if not usable else "CHECK" if check else "DELAYED" if impacts else "OK"
        return {"status": status, "impacts": impacts,
                "minutes_with_events": round(plan.minutes + delay) if usable else None}

    @staticmethod
    def compare(cand: Plan, orig: Plan, orig_check: dict) -> dict:
        cb = [l for l in cand.legs if l.mode == "bus"]
        ob = [l for l in orig.legs if l.mode == "bus"]
        cn, on = [l.route for l in cb], [l.route for l in ob]
        rides = {(l.route, s) for l in cb for s in range(l.board_seq, l.alight_seq + 1)}
        avoided = []
        for im in orig_check["impacts"]:
            if im["role"] == "VIA":
                hit = (im["route"], im["seq"]) in rides
            else:
                hit = any(l.route == im["route"] and im["seq"] in (l.board_seq, l.alight_seq) for l in cb)
            if not hit:
                avoided.append(f"{im['route']}번 {im['stop']}({im['effect'].split(' ·')[0]})")
        diff = lambda a, b: a if a != b else None
        out = {
            "same_as_original": cn == on and all(a.board_seq == b.board_seq and a.alight_seq == b.alight_seq
                                                 for a, b in zip(cb, ob)),
            "d_minutes": round(cand.minutes) - round(orig.minutes),
            "d_transfers": cand.transfers - orig.transfers,
            "d_walk_m": round(cand.walk_m - orig.walk_m),
            "buses_removed": [r for r in on if r not in cn],
            "buses_added": [r for r in cn if r not in on],
            "buses_kept": [r for r in cn if r in on],
            "first_board": diff(cb[0].from_name if cb else None, ob[0].from_name if ob else None),
            "last_alight": diff(cb[-1].to_name if cb else None, ob[-1].to_name if ob else None),
            "avoided": avoided,
        }
        if orig_check["minutes_with_events"] is not None:
            out["d_minutes_vs_event"] = round(cand.minutes) - orig_check["minutes_with_events"]
        return out

    # search effort: (baseline runs, strict runs, relaxed runs). "fast" for weak servers.
    EFFORT = {"full": (4, 8, 3), "fast": (2, 5, 1)}

    def search(self, olat, olng, dlat, dlng, depart_min: float, top_k: int = 5,
               subway: bool = True, effort: str = "full") -> dict:
        nb, ns, nr = self.EFFORT.get(effort, self.EFFORT["full"])
        a = self.net.a
        o, d = (olat, olng), (dlat, dlng)
        self._ban0 = frozenset() if subway else frozenset(r for r, k in self.net.kind.items() if k == "subway")
        # 1) original routes (events ignored) and T0* = fastest original, fixed
        base = sorted(self._diverse(o, d, depart_min, "baseline", None, max_runs=nb),
                      key=lambda p: (p.minutes, p.transfers, p.walk_m))
        if not base:
            return {"t0_min": None, "confirmed": [], "needs_check": [], "original": [],
                    "message": "출발지·도착지 400m 안에서 연결되는 버스 경로를 찾지 못했습니다."}
        t0 = base[0].minutes
        limit = t0 + a.max_extra_min
        budget = depart_min + limit
        # 2) sequential role-based check of each original
        originals = []
        for p in base[:3]:
            chk = self.check_original(p)
            originals.append((p, chk))
        main, main_chk = originals[0]
        # 3) event-aware candidates (branches are inside the search: walk to the
        #    same route's neighbour stop, ride on past a skipped stop, other routes)
        keep = lambda p: p.minutes <= limit and p.transfers <= a.max_transfers
        strict = [p for p in self._diverse(o, d, depart_min, "strict", budget, ns, limit) if keep(p)]
        confirmed = [p for p in strict if not p.needs_check]
        fastest_ok = min((p.minutes for p in confirmed), default=math.inf)
        relaxed = self._diverse(o, d, depart_min, "relaxed", budget, nr, limit)
        needs = [p for p in relaxed if keep(p) and p.needs_check and p.minutes < fastest_ok]
        # 4) Pareto ranking (front number, then time)
        def pack(ps, k):
            out = []
            for p, front in _pareto_rank(ps)[:k]:
                dct = p.to_dict()
                dct["pareto_rank"] = front
                dct["compare"] = self.compare(p, main, main_chk)
                out.append(dct)
            return out
        orig_out = []
        for p, chk in originals:
            dct = p.to_dict()
            dct.update(check=chk)
            orig_out.append(dct)
        return {"t0_min": round(t0), "limit_min": round(limit), "subway": subway,
                "original": orig_out,
                "original_status": main_chk["status"],
                "original_affected": main_chk["status"] != "OK",
                "confirmed": pack(confirmed, top_k),
                "needs_check": pack(needs, 2),
                "assumptions": asdict(a), "estimated": True}


def _pareto_rank(plans: list[Plan]) -> list[tuple[Plan, int]]:
    """Non-dominated sorting on (minutes, transfers, walk); returns (plan, front) sorted."""
    key = lambda p: (round(p.minutes), p.transfers, round(p.walk_m))
    left, out, front = list(plans), [], 1
    while left:
        nd = [p for p in left
              if not any(all(x <= y for x, y in zip(key(q), key(p))) and key(q) != key(p) for q in left)]
        out.extend((p, front) for p in sorted(nd, key=key))
        left = [p for p in left if p not in nd]
        front += 1
    return out


def _pareto(plans: list[Plan]) -> list[Plan]:
    key = lambda p: (p.minutes, p.transfers, p.walk_m)
    return [p for p in plans
            if not any(all(x <= y for x, y in zip(key(q), key(p))) and key(q) != key(p) for q in plans)]


# --------------------------------------------------------------------------- #
# map layer: active controls at a time
# --------------------------------------------------------------------------- #
def controls(net: Network, minute: float) -> dict:
    stops, segments = [], []
    for rid, vs in net.visits.items():
        run: list[int] = []
        def flush():
            if not run:
                return
            lo, hi = max(run[0] - 1, 0), min(run[-1] + 1, len(vs) - 1)
            segments.append({"route": net.routes[rid],
                             "path": [[v.lat, v.lng] for v in vs[lo:hi + 1]]})
            run.clear()
        for i, v in enumerate(vs):
            st = net.status(rid, i, minute) if not v.virtual else None
            if st and st.state in ("CONTROLLED", "CHECK"):
                run.append(i)
                stops.append({"route": net.routes[rid], "id": v.ars, "seq": v.seq, "name": v.name,
                              "lat": v.lat, "lng": v.lng,
                              "status": "controlled" if st.state == "CONTROLLED" else "check",
                              "until": min_to_hhmm(max(w.end for w in st.windows)),
                              "pdf_page": st.windows[0].pdf_page})
            elif not v.virtual:
                flush()
        flush()
    return {"time": min_to_hhmm(minute), "stops": stops, "segments": segments}
