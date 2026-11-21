"""Load the team's skipped-stop CSV (format v1) into the `events` table.

Rules from the data hand-off (2026-11-21):
  * read by column NAME, UTF-8 (BOM), every ID kept as a string (e.g. '02605')
  * only `*_active.csv` is used; `*_hold.csv` and `*_우회경로.csv` are not loaded
  * rows with an empty 정류소순번 are NOT used for control decisions (direction is
    estimated and a same-name stop exists on the other side). They are kept with
    stop_sequence='' so the citizen screen can show a caution note only.
  * 종료시각 empty + 종료시각구분 '통제해제시' = end unknown (engine estimates it)
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .ingest import DDL

REQUIRED = ["노선번호", "방향", "방향근거", "우회일자", "시작시각", "종료시각", "종료시각구분",
            "무정차정류소", "정류소ID", "정류소순번", "확인상태", "비고", "PDF쪽"]


def read_active(path: str | Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{Path(path).name}: missing columns {missing}")
        return [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]


def load_active(db_path: str | Path, csv_path: str | Path) -> dict:
    rows = read_active(csv_path)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(DDL)
        stats: Counter[str] = Counter()
        with con:
            con.execute("DELETE FROM events")
            for i, r in enumerate(rows, start=1):
                route, ars, seq = r["노선번호"], r["정류소ID"], r["정류소순번"]
                matched = 0
                if ars and seq.isdigit():
                    hit = con.execute("SELECT ars_id FROM route_stops WHERE route_name=? AND seq=?",
                                      (route, int(seq))).fetchone()
                    matched = int(bool(hit) and hit[0] == ars)
                    stats["seq_ok" if matched else "seq_mismatch"] += 1
                else:
                    stats["seq_blank_not_used"] += 1
                end = r["종료시각"] or r["종료시각구분"]
                con.execute("""INSERT INTO events (event_id,route_name,direction,direction_basis,
                    event_date,start_text,end_text,old_route_text,detour_route_text,skipped_stop_name,
                    ars_id,id_source,stop_sequence,notice_stop_count,review_state,notes,pdf_page,
                    route_stop_match,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    i, route, r["방향"], r["방향근거"], r["우회일자"], r["시작시각"], end,
                    r.get("기존", ""), r.get("우회", ""), r["무정차정류소"], ars, r.get("ID출처", ""),
                    seq, r.get("공지상정류소수", ""), r["확인상태"], r["비고"], r["PDF쪽"],
                    matched, json.dumps(r, ensure_ascii=False)))
                stats["rows"] += 1
            con.execute("INSERT OR REPLACE INTO metadata VALUES ('event_source', ?)", (Path(csv_path).name,))
            dates = sorted({r["우회일자"] for r in rows})
            con.execute("INSERT OR REPLACE INTO metadata VALUES ('event_dates', ?)", (",".join(dates),))
        return dict(stats) | {"source": Path(csv_path).name, "dates": dates}
    finally:
        con.close()
