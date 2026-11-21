"""Subway network built from the uploaded Seoul Metro CSVs (real data in data/subway)."""
import os
import unittest

from cityflow.planner import Network, Planner
from cityflow.subway import _hav, load_lines

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "cityflow.db")


class SubwayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = Network(DB)

    def test_lines_and_shapes(self):
        d = load_lines(os.path.join(HERE, "data", "subway"))
        lines = {line for _, line, _, _ in d["routes"]}
        self.assertEqual(lines, set("12345678"))
        for _, _, _, seq in d["routes"]:                 # no jumps from bad coordinates
            for a, b in zip(seq, seq[1:]):
                self.assertLess(_hav(a[1:3], b[1:3]), 3500)

    def test_subway_never_controlled(self):
        net = self.net
        for rid, k in net.kind.items():
            if k != "subway":
                continue
            for i in range(len(net.visits[rid])):
                self.assertEqual(net.status(rid, i, 9 * 60).state, "NORMAL")

    def test_interchange_walk_links(self):
        linked = {o for o, _ in self.net.walk["S1:종로3가"]}
        self.assertTrue({"S3:종로3가", "S5:종로3가"} <= linked)

    def test_search_uses_subway_and_can_exclude_it(self):
        P = Planner(self.net)
        o, d = (37.5759, 126.9768), (37.5133, 127.1001)           # 광화문 -> 잠실
        on = P.search(*o, *d, 8 * 60, subway=True)
        off = P.search(*o, *d, 8 * 60, subway=False)
        self.assertTrue(any(l["kind"] == "subway" for l in on["original"][0]["legs"]))
        self.assertFalse(any(l["kind"] == "subway" for c in off["confirmed"] for l in c["legs"]))
        self.assertLess(on["t0_min"], off["t0_min"])


if __name__ == "__main__":
    unittest.main()
