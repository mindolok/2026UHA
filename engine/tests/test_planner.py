"""Synthetic network tests for the sequence-based planner. Times are estimates."""
import os
import sqlite3
import tempfile
import unittest

from cityflow.ingest import DDL
from cityflow.planner import Assumptions, Network, Planner, stop_check

LAT = 37.5
STEP = 0.0057   # ~500 m in longitude at this latitude


def build(events, b_route=True):
    """Route A: 8 stops west->east on LAT (seq 4 is virtual).
    Route B: same corridor 200 m north, 4 stops (optional)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(DDL)
    for i in range(1, 9):
        name = f"A{i}(가상)" if i == 4 else f"A{i}"
        con.execute("INSERT INTO route_stops VALUES(?,?,?,?,?,?,?,?)",
                    ("RA", "A", i, f"N{i}", f"0100{i}", name, 127.0 + STEP * i, LAT))
    if b_route:
        for i, x in enumerate((1, 3, 5, 7), start=1):
            con.execute("INSERT INTO route_stops VALUES(?,?,?,?,?,?,?,?)",
                        ("RB", "B", i, f"M{i}", f"0200{i}", f"B{i}", 127.0 + STEP * x, LAT + 0.0018))
    for n, (seq, start, end, review) in enumerate(events, start=1):
        con.execute("""INSERT INTO events(event_id,route_name,direction,direction_basis,event_date,
            start_text,end_text,skipped_stop_name,ars_id,stop_sequence,review_state,pdf_page,
            route_stop_match,raw_json,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (n, "A", "동쪽 방향", "원문", "2026-11-01", start, end, f"A{seq}",
                     f"0100{seq}", str(seq), review, "1", 1, "{}", ""))
    con.commit()
    con.close()
    return path


def pt(i, north=0.0):
    return LAT + north, 127.0 + STEP * i


class PlannerTests(unittest.TestCase):
    def net(self, events, **kw):
        p = build(events, **kw)
        self.addCleanup(os.remove, p)
        return Network(p, Assumptions(wait_s=60))

    def test_sequence_match_and_time_window(self):
        net = self.net([(3, "08:00", "09:00", "확인")])
        self.assertEqual(net.status("RA", 2, 8 * 60 + 30).state, "CONTROLLED")
        self.assertEqual(net.status("RA", 2, 9 * 60 + 1).state, "NORMAL")
        self.assertEqual(net.status("RA", 1, 8 * 60 + 30).state, "NORMAL")

    def test_review_needed_is_check_and_virtual_blocked(self):
        net = self.net([(3, "08:00", "09:00", "확인 필요")])
        self.assertEqual(net.status("RA", 2, 8 * 60 + 30).state, "CHECK")
        self.assertTrue(net.status("RA", 3, 8 * 60).blocked)          # (가상)

    def test_unbounded_end_uses_max_plus_20(self):
        net = self.net([(3, "08:00", "13:00", "확인"), (5, "08:00", "통제해제시", "확인")])
        self.assertEqual(net.unbounded_end, 13 * 60 + 20)
        self.assertEqual(net.status("RA", 4, 13 * 60 + 15).state, "CONTROLLED")

    def test_alight_pivots_skip_virtual_and_blocked(self):
        net = self.net([(5, "08:00", "09:00", "확인"), (6, "08:00", "09:00", "확인")])
        p = net.pivots("RA", 4, 8 * 60 + 30)                           # A5
        self.assertEqual(net.visits["RA"][p["before"]].seq, 3)        # A4 is virtual
        self.assertEqual(net.visits["RA"][p["after"]].seq, 7)
        r = stop_check(net, "A", "01005", 8 * 60 + 30)
        self.assertEqual(r["status"], "CONTROLLED")
        self.assertEqual([x["seq"] for x in r["visits"][0]["alternatives"]], [3, 7])

    def test_pass_through_adds_delay_once_per_block(self):
        base = self.net([], b_route=False)
        ev = self.net([(5, "00:00", "23:59", "확인"), (6, "00:00", "23:59", "확인")], b_route=False)
        o, d = pt(1), pt(8)
        t_base = Planner(base).search(*o, *d, 8 * 60)["confirmed"][0]["minutes"]
        r = Planner(ev).search(*o, *d, 8 * 60)
        c = r["confirmed"][0]
        self.assertEqual(c["minutes"] - t_base, 10)                    # one block, +10 once
        self.assertEqual(len(c["legs"][1]["passed_blocks"]), 1)

    def test_boarding_skip_walks_to_neighbor_stop(self):
        net = self.net([(2, "00:00", "23:59", "확인")], b_route=False)
        r = Planner(net).search(*pt(2), *pt(8), 8 * 60)
        bus = [l for l in r["confirmed"][0]["legs"] if l["mode"] == "bus"][0]
        self.assertNotEqual(bus["board_seq"], 2)

    def test_check_section_excluded_from_confirmed(self):
        net = self.net([(5, "00:00", "23:59", "확인 필요")], b_route=False)
        r = Planner(net).search(*pt(1), *pt(8), 8 * 60)
        self.assertTrue(all(not p["needs_check"] for p in r["confirmed"]))
        self.assertTrue(r["needs_check"])
        self.assertTrue(r["needs_check"][0]["reasons"])

    def test_alternative_route_and_60_min_limit(self):
        net = self.net([(5, "00:00", "23:59", "확인 필요")])
        r = Planner(net).search(*pt(1), *pt(8), 8 * 60)
        self.assertEqual(r["limit_min"], r["t0_min"] + 60)
        for p in r["confirmed"]:
            self.assertLessEqual(p["minutes"], r["limit_min"])
            self.assertLessEqual(p["transfers"], 5)
        self.assertTrue(any("B" in p["summary"] for p in r["confirmed"]))

    def test_original_sequential_check_roles(self):
        net = self.net([(1, "00:00", "23:59", "확인"), (5, "00:00", "23:59", "확인")], b_route=False)
        P = Planner(net)
        base = P._plans(*pt(1), *pt(8), 8 * 60, "baseline", None)
        orig = [p for p in base if any(l.mode == "bus" for l in p.legs)][0]
        chk = P.check_original(orig)
        roles = {i["role"] for i in chk["impacts"]}
        self.assertIn("VIA", roles)                     # A5 passed through
        if orig.legs[1].board_seq == 1:
            self.assertEqual(chk["status"], "BLOCKED")  # boarding at skipped A1

    def test_multiple_candidates_pareto_and_compare(self):
        net = self.net([(2, "00:00", "23:59", "확인")])
        r = Planner(net).search(*pt(1), *pt(8), 8 * 60)
        self.assertGreaterEqual(len(r["confirmed"]), 2)
        self.assertTrue(all("compare" in c and "pareto_rank" in c for c in r["confirmed"]))
        ranks = [c["pareto_rank"] for c in r["confirmed"]]
        self.assertEqual(ranks, sorted(ranks))
        sigs = [c["summary"] for c in r["confirmed"]]
        self.assertEqual(len(sigs), len(set(sigs)))       # distinct bus sequences
        for c in r["confirmed"]:
            self.assertLessEqual(c["minutes"], r["limit_min"])


if __name__ == "__main__":
    unittest.main()
