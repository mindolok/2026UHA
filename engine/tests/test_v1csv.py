"""Loader tests for the team's v1 CSV format (synthetic rows)."""
import csv
import os
import sqlite3
import tempfile
import unittest

from cityflow.ingest import DDL
from cityflow.planner import Network, stop_check
from cityflow.v1csv import load_active

COLS = ["비고", "노선번호", "방향", "방향근거", "우회일자", "시작시각", "종료시각", "종료시각구분",
        "무정차정류소", "정류소ID", "정류소순번", "확인상태", "PDF쪽", "기타열"]   # order shuffled on purpose


def make(rows):
    d = tempfile.mkdtemp()
    db, f = os.path.join(d, "t.db"), os.path.join(d, "a.csv")
    con = sqlite3.connect(db); con.executescript(DDL)
    for i, (ars, name) in enumerate([("02605", "A1"), ("02606", "A2"), ("02607", "A3")], start=1):
        con.execute("INSERT INTO route_stops VALUES(?,?,?,?,?,?,?,?)", ("R", "100", i, f"N{i}", ars, name, 127 + i * .005, 37.5))
    con.commit(); con.close()
    with open(f, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS); w.writeheader()
        for r in rows:
            w.writerow({c: "" for c in COLS} | {"노선번호": "100", "방향": "동쪽", "방향근거": "원문",
                        "우회일자": "2026-11-01", "확인상태": "확인", "PDF쪽": "1"} | r)
    return db, f


class V1CsvTests(unittest.TestCase):
    def test_columns_by_name_ids_as_strings_and_blank_seq_excluded(self):
        db, f = make([
            {"정류소ID": "02606", "정류소순번": "2", "시작시각": "08:00", "종료시각": "09:00", "종료시각구분": "지정", "무정차정류소": "A2"},
            {"정류소ID": "02605", "정류소순번": "", "시작시각": "08:00", "종료시각": "09:00", "종료시각구분": "지정", "무정차정류소": "A1"},
            {"정류소ID": "02607", "정류소순번": "3", "시작시각": "08:00", "종료시각": "", "종료시각구분": "통제해제시", "무정차정류소": "A3"},
        ])
        stats = load_active(db, f)
        self.assertEqual(stats["seq_ok"], 2)
        self.assertEqual(stats["seq_blank_not_used"], 1)
        net = Network(db)
        self.assertEqual(net.status("R", 1, 8 * 60 + 30).state, "CONTROLLED")   # 02606 seq 2
        self.assertEqual(net.status("R", 0, 8 * 60 + 30).state, "NORMAL")       # blank seq: not used
        r = stop_check(net, "100", "02605", 8 * 60 + 30)
        self.assertEqual(r["status"], "NORMAL")
        self.assertEqual(len(r["uncertain_notices"]), 1)                        # caution only
        self.assertTrue(net.windows[("R", 3)][0].end_estimated)                 # 통제해제시

    def test_missing_column_rejected(self):
        d = tempfile.mkdtemp(); f = os.path.join(d, "bad.csv")
        open(f, "w", encoding="utf-8-sig").write("노선번호,정류소ID\n100,02605\n")
        db, _ = make([])
        with self.assertRaises(ValueError):
            load_active(db, f)


if __name__ == "__main__":
    unittest.main()
