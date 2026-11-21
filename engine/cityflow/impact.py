"""Conservative, source-backed planned no-stop checks."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from .ingest import normalize_ars

SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")

@dataclass(frozen=True)
class Impact:
    status: str
    route_name: str
    stop_id: str
    time: str
    direction: str | None
    evidence: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def parse_local_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset, e.g. 2026-11-01T09:00:00+09:00")
    return timestamp.astimezone(SEOUL)


def _clock_minutes(text: str) -> int | None:
    try:
        if not text or len(text) != 5 or text[2] != ":":
            return None
        hour, minute = (int(x) for x in text.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return hour * 60 + minute
    except ValueError:
        return None


def check_impact(db: sqlite3.Connection, route_name: str, stop_id: str,
                 at_iso: str, direction: str | None) -> Impact:
    when = parse_local_timestamp(at_iso)
    ars = normalize_ars(stop_id)
    if not ars:
        return Impact("INVALID_STOP_ID",route_name,stop_id,at_iso,direction)
    rows = db.execute("""SELECT direction,direction_basis,event_date,start_text,end_text,
                     skipped_stop_name,ars_id,review_state,pdf_page,route_stop_match,
                     notes,event_id FROM events WHERE route_name=? AND ars_id=? AND event_date=?""",
                     (route_name,ars,when.date().isoformat())).fetchall()
    if not rows:
        return Impact("NO_MATCHING_NOTICE",route_name,ars,at_iso,direction,
                      warnings=("No matching row is not proof of normal service.",))
    if not direction:
        return Impact("DIRECTION_REQUIRED",route_name,ars,at_iso,None,
                      warnings=("Direction must be supplied explicitly; the source XLSX has no direction column.",))
    evidence: list[dict] = []
    warnings: list[str] = []
    for (ev_dir,dir_basis,date,start,end,name,ev_stop,status,page,master_ok,note,event_id) in rows:
        if ev_dir != direction:
            continue
        start_min,end_min = _clock_minutes(start),_clock_minutes(end)
        current = when.hour*60+when.minute
        if start_min is None:
            evidence.append({"event_id":event_id,"pdf_page":page,"status":"UNKNOWN_START", "stop_name":name})
            warnings.append("Unparseable start time in notice.")
            continue
        if current < start_min:
            continue
        if end_min is not None and current > end_min:
            continue
        # A textual end such as '통제해제시' does not establish when restrictions ended.
        confident = status == "확인" and dir_basis == "원문" and master_ok == 1 and end_min is not None
        evidence.append({
            "event_id":event_id,"pdf_page":page,"source_direction":ev_dir,
            "source_direction_basis":dir_basis,"start":start,"end":end,
            "stop_name":name,"source_review_status":status,
            "route_stop_id_match":bool(master_ok),"notes":note,
            "row_evidence_strength":"SOURCE_AND_MASTER_MATCHED" if confident else "REQUIRES_REVIEW"
        })
        if not confident:
            warnings.append("Direction, time window, ID match, or source review needs manual verification.")
    if not evidence:
        return Impact("NO_MATCHING_NOTICE",route_name,ars,at_iso,direction,
                      warnings=("No active matching row; this is not a guarantee of service.",))
    if all(e.get("row_evidence_strength") == "SOURCE_AND_MASTER_MATCHED" for e in evidence):
        status = "PLANNED_SKIP"
    else:
        status = "POSSIBLE_SKIP_REVIEW"
    return Impact(status,route_name,ars,at_iso,direction,tuple(evidence),tuple(sorted(set(warnings))))
