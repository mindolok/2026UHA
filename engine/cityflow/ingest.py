"""Import uploaded bus-stop spreadsheets and event CSV into SQLite.

Important: event status '확인' is *not* a gold-standard safety certificate.
It records the source CSV review field, and matching is checked separately.
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
import sqlite3
from collections import Counter
from .xlsx_stream import iter_dicts

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    route_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
    node_id TEXT PRIMARY KEY,
    ars_id TEXT NOT NULL,
    name TEXT NOT NULL,
    lng REAL,
    lat REAL,
    kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_stops_ars ON stops(ars_id);
CREATE TABLE IF NOT EXISTS route_stops (
    route_id TEXT NOT NULL,
    route_name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    ars_id TEXT NOT NULL,
    name TEXT NOT NULL,
    lng REAL,
    lat REAL,
    PRIMARY KEY (route_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_rs_name_ars ON route_stops(route_name, ars_id);
CREATE INDEX IF NOT EXISTS idx_rs_route_seq ON route_stops(route_id, seq);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    route_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    direction_basis TEXT,
    event_date TEXT NOT NULL,
    start_text TEXT NOT NULL,
    end_text TEXT NOT NULL,
    old_route_text TEXT,
    detour_route_text TEXT,
    skipped_stop_name TEXT,
    ars_id TEXT,
    id_source TEXT,
    stop_sequence TEXT,
    notice_stop_count TEXT,
    review_state TEXT,
    notes TEXT,
    pdf_page TEXT,
    route_stop_match INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_route_stop ON events(route_name, ars_id, event_date);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def normalize_ars(value: str | int | None) -> str:
    """Normalize a valid Seoul ARS stop number without conflating NODE_ID."""
    text = str(value or "").strip().replace("-", "")
    if not text or not text.isdigit() or len(text) > 5:
        return ""
    return text.zfill(5)


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def create_database(routes_xlsx: str, stops_xlsx: str, events_csv: str, target: str) -> dict[str, object]:
    p = Path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(str(p))
    try:
        con.executescript(DDL)
        stats: Counter[str] = Counter()
        with con:
            for row in iter_dicts(stops_xlsx):
                ars_id = normalize_ars(row.get("ARS_ID", ""))
                if not row.get("NODE_ID") or not ars_id:
                    stats["stops_missing_key"] += 1
                    continue
                con.execute("INSERT OR REPLACE INTO stops VALUES(?, ?, ?, ?, ?, ?)", (
                    row["NODE_ID"], ars_id, row.get("정류소명", ""),
                    _num(row.get("X좌표", "")), _num(row.get("Y좌표", "")),
                    row.get("정류소타입", "")))
                stats["stop_rows"] += 1
            for row in iter_dicts(routes_xlsx):
                ars_id = normalize_ars(row.get("ARS_ID", ""))
                try:
                    seq = int(float(row.get("순번", "")))
                except (ValueError, TypeError):
                    stats["route_rows_invalid_seq"] += 1
                    continue
                route_id, name = row.get("ROUTE_ID", ""), row.get("노선명", "")
                if not route_id or not name or not ars_id:
                    stats["route_rows_missing_key"] += 1
                    continue
                con.execute("INSERT OR IGNORE INTO routes VALUES (?, ?)", (route_id, name))
                con.execute("INSERT OR REPLACE INTO route_stops VALUES (?,?,?,?,?,?,?,?)", (
                    route_id, name, seq, row.get("NODE_ID", ""), ars_id,
                    row.get("정류소명", ""), _num(row.get("X좌표", "")),
                    _num(row.get("Y좌표", ""))))
                stats["route_stop_rows"] += 1
            with open(events_csv, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    route, ars = row["노선번호"].strip(), normalize_ars(row["정류소ID"])
                    matched = 0
                    if ars:
                        matched = int(con.execute(
                            "SELECT EXISTS(SELECT 1 FROM route_stops WHERE route_name = ? AND ars_id = ?)",
                            (route, ars)).fetchone()[0])
                    con.execute("""INSERT INTO events (
                        route_name,direction,direction_basis,event_date,start_text,end_text,
                        old_route_text,detour_route_text,skipped_stop_name,ars_id,
                        id_source,stop_sequence,notice_stop_count,review_state,notes,
                        pdf_page,route_stop_match,raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        route,row["방향"],row["방향근거"],row["우회일자"],
                        row["시작시각"],row["종료시각"],row["기존"],row["우회"],
                        row["무정차정류소"],ars,row["ID출처"],row["정류소순번"],
                        row["공지상정류소수"],row["확인상태"],row["비고"],row["PDF쪽"],
                        matched,json.dumps(row,ensure_ascii=False)))
                    stats["event_rows"] += 1
                    if not matched:
                        stats["events_not_matching_route_master"] += 1
                    if row["확인상태"] != "확인":
                        stats["events_marked_review"] += 1
                    if row["방향근거"] != "원문":
                        stats["events_direction_not_literal"] += 1
        metadata = {
            "route_data_date": "2026-09-02", "event_source": Path(events_csv).name,
            "event_scope": "2026-11-01 JTBC Seoul Marathon planned notices"
        }
        with con:
            for k, v in metadata.items():
                con.execute("INSERT INTO metadata VALUES (?,?)", (k,v))
        return dict(stats) | {"database": str(p)}
    finally:
        con.close()
