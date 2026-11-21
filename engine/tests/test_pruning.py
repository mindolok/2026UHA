import unittest
from cityflow.pruning import DominanceIndex,StateLabel,prune_by_budget


class StatePruningTests(unittest.TestCase):
    def test_early_prune_exact_boundary_allowed(self):
        self.assertFalse(prune_by_budget(70,35,45,60))
        self.assertTrue(prune_by_budget(70,35.01,45,60))

    def test_unknown_lower_bound_is_safe_zero(self):
        self.assertFalse(prune_by_budget(50,None,45,60))

    def test_comparable_bus_state_only(self):
        common={"stop_id":"05108","current_line_id":"240",
                "direction":"신사역 방향","current_trip_id":"demo-trip-1",
                "next_stop_seq":34,"destination_key":"demo-target",
                "event_date":"2026-11-01","root_id":"R1"}
        ix=DominanceIndex("global_fastest")
        first=StateLabel(**common,elapsed_minutes=20,transfers=1,walk_distance_m=100)
        slower=StateLabel(**common,elapsed_minutes=25,transfers=2,walk_distance_m=120)
        self.assertFalse(ix.dominated_or_add(first))
        self.assertTrue(ix.dominated_or_add(slower))
        other_line=StateLabel(**(common|{"current_line_id":"262"}),
                              elapsed_minutes=30,transfers=2,walk_distance_m=200)
        self.assertFalse(ix.dominated_or_add(other_line))

if __name__=='__main__':unittest.main()
