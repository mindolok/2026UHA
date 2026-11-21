"""Seoul Metro lines 1-8 as directed 'routes' for the planner.

Sources (UTF-8 copies in data/subway/):
  subway_times.csv   서울교통공사 역간거리 및 소요시간 (line, station order, run time from previous)
  subway_coords.csv  서울교통공사 1~8호선 역사 좌표
Subway is never checked against bus control notices (team decision).

Shape handling:
  * a row whose straight-line distance to the previous row does not fit its
    역간거리 starts a branch; its predecessor is the earlier station whose
    distance best matches (5호선 마천지선, 2호선 성수·신정지선)
  * 2호선 main loop: inner/outer circle, each running 1.5 laps so any loop trip fits
  * 2호선 branches run on their own (transfer at 성수/신도림)
  * other branches run through from the line's first station (5호선 방화→마천)
  * 6호선 응암 loop is one-way (응암→역촌→…→구산→응암)
"""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path

ALIAS = {("1", "서울역"): "서울", ("4", "서울역"): "서울", ("6", "신내역"): "신내",
         ("7", "자양"): "뚝섬유원지", ("7", "이수"): "총신대입구"}
# Known errors in the coordinate file (checked 2026-11-21):
#   2호선 잠실새내 ≈ 잠실나루 (10 m apart), 5호선 발산 == 마곡, 2호선 용답 near 을지로입구 and
#   신답 holds a point 2.3 km from 성수 (= 용답's 역간거리). Fix: 용답 takes that point; the others
#   are placed between their neighbours in proportion to 역간거리 (no outside coordinates).
COORD_FROM = {("2", "용답"): ("2", "신답")}
COORD_INTERPOLATE = {("2", "잠실새내"), ("2", "신답"), ("5", "발산")}
# branch first station -> station it leaves from
BRANCH_START = {("2", "용답"): "성수", ("2", "도림천"): "신도림", ("5", "둔촌동"): "강동"}
LINE_COLOR = {"1": "#0052A4", "2": "#00A84D", "3": "#EF7C1C", "4": "#00A5DE",
              "5": "#996CAC", "6": "#CD7C2F", "7": "#747F00", "8": "#E6186C"}


def _norm(s: str) -> str:
    return re.sub(r"\(.*?\)", "", s).strip()


def _hav(a, b) -> float:
    (la1, ln1), (la2, ln2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(ln2 - ln1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _secs(t: str) -> int:
    m, s = t.strip().split(":")
    return int(m) * 60 + int(s)


def load_lines(folder: str | Path) -> dict:
    """Return {'routes': [(route_id, line, [(name, lat, lng, run_s_from_prev), ...])], 'skipped': [...]}"""
    folder = Path(folder)
    times = list(csv.DictReader(open(folder / "subway_times.csv", encoding="utf-8-sig")))
    coords = list(csv.DictReader(open(folder / "subway_coords.csv", encoding="utf-8-sig")))
    raw = {(r["호선"].strip(), _norm(r["역명"])): (float(r["위도"]), float(r["경도"])) for r in coords}
    pos = dict(raw)
    for k, src in COORD_FROM.items():
        pos[k] = raw[src]
    skipped, rows_by_line = [], {}
    for r in times:
        line, name = r["호선"].strip(), r["역명"].strip()
        key = ALIAS.get((line, name), _norm(name))
        p = pos.get((line, key))
        if p is None:
            skipped.append(f"{line}호선 {name}")
            continue
        rows_by_line.setdefault(line, []).append(
            {"name": _norm(name), "pos": p, "run": _secs(r["소요시간"]), "km": float(r["역간거리(km)"] or 0)})

    # interpolate known-bad coordinates between neighbours by 역간거리
    for line, rows in rows_by_line.items():
        for i, r in enumerate(rows):
            if (line, r["name"]) in COORD_INTERPOLATE and 0 < i < len(rows) - 1:
                a, b = rows[i - 1]["pos"], rows[i + 1]["pos"]
                f = r["km"] / max(r["km"] + rows[i + 1]["km"], 1e-6)
                r["pos"] = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    routes = []
    for line, rows in rows_by_line.items():
        trunk, branches = [rows[0]], []
        cur = trunk
        for r in rows[1:]:
            attach = BRANCH_START.get((line, r["name"]))
            if attach:
                base = next(x for x in rows if x["name"] == attach)
                cur = [dict(base, run=0), r]
                branches.append(cur)
            else:
                cur.append(r)
        seqs = []
        if line == "2":
            ring = trunk[:-1] if trunk[-1]["name"] == trunk[0]["name"] else trunk   # 시청 ... 충정로
            close = trunk[-1]["run"] if trunk[-1]["name"] == trunk[0]["name"] else 120
            outer = ring + [dict(ring[0], run=close)] + ring[1:len(ring) // 2 + 1]
            seqs.append(("외선", outer))
            seqs.append(("내선", _reverse(outer)))
            for b in branches:
                seqs += [(b[-1]["name"] + "행", b), (b[0]["name"] + "행", _reverse(b))]
        elif line == "6":
            # 응암(0) 역촌 불광 독바위 연신내 구산 응암(6) 새절 ...
            loop_end = next(i for i in range(1, len(trunk)) if trunk[i]["name"] == trunk[0]["name"])
            seqs.append((trunk[-1]["name"] + "행", trunk[loop_end:]))
            rev = _reverse(trunk[loop_end:]) + trunk[1:loop_end + 1]
            seqs.append(("응암순환", rev))
            seqs.append(("응암순환(시작)", trunk[: loop_end + 1]))
        else:
            seqs += [(trunk[-1]["name"] + "행", trunk), (trunk[0]["name"] + "행", _reverse(trunk))]
            for b in branches:
                i = next(k for k, x in enumerate(trunk) if x["name"] == b[0]["name"])
                full = trunk[: i + 1] + b[1:]
                seqs += [(full[-1]["name"] + "행", full), (full[0]["name"] + "행", _reverse(full))]
        for k, (label, seq) in enumerate(seqs):
            routes.append((f"SUB{line}-{k}", line, label,
                           [(x["name"], x["pos"][0], x["pos"][1], x["run"]) for x in seq]))
    return {"routes": routes, "skipped": skipped}


def _reverse(seq: list[dict]) -> list[dict]:
    """Same stations in reverse; each run time moves to the station it now arrives at."""
    out = []
    rev = list(reversed(seq))
    for i, x in enumerate(rev):
        run = 0 if i == 0 else rev[i - 1]["run"]
        out.append(dict(x, run=run))
    return out
