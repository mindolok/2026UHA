"""Synthetic tests. Times are hypothetical, never operational bus predictions."""
import sqlite3
import unittest
from cityflow.impact import check_impact
from cityflow.ingest import normalize_ars
from cityflow.routing import Candidate, CatalogAlternativeProvider, evaluate_candidates


def db_fixture():
    db=sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE route_stops(route_name TEXT,ars_id TEXT);
        CREATE TABLE events(
            direction TEXT,direction_basis TEXT,event_date TEXT,
            start_text TEXT,end_text TEXT,skipped_stop_name TEXT,
            ars_id TEXT,review_state TEXT,pdf_page TEXT,route_stop_match INTEGER,
            notes TEXT,event_id INTEGER,route_name TEXT
        );
        INSERT INTO route_stops VALUES('240','05108');
        INSERT INTO route_stops VALUES('147','11486');
        INSERT INTO events VALUES('신사역 방향','원문','2026-11-01','07:59','11:31',
            '중곡1동사거리','05108','확인','17',1,'',1,'240');
    """)
    return db


def candidate(id,root,duration_min,route,stop,time,role="BOARD",parent=None,pivot=None,calls=0,transfers=0):
    from datetime import datetime,timedelta
    dep=datetime.fromisoformat('2026-11-01T08:40:00+09:00')
    arrive=(dep+timedelta(minutes=duration_min)).isoformat()
    return Candidate.from_dict({
        "candidate_id":id,"root_id":root,"depart_at":dep.isoformat(),
        "arrive_at":arrive,"walk_distance_m":0,"parent_id":parent,
        "pivot_stop_id":pivot,"reroute_calls":calls,"service_evidence":"VERIFIED",
        "visits":[{"route_name":route,"stop_id":stop,"time":time,
                   "direction":"신사역 방향","role":role}]
        + [{"route_name":route,"stop_id":stop,"time":time,
                   "direction":"신사역 방향","role":"TRANSFER_BOARD"}
           for _ in range(transfers)]
    })


class CityflowTests(unittest.TestCase):
    def test_ars_format(self):
        self.assertEqual(normalize_ars('02-605'),'02605')
        self.assertEqual(normalize_ars('05108'),'05108')
        self.assertEqual(normalize_ars('123456789'),'')

    def test_event_and_time(self):
        db=db_fixture()
        x=check_impact(db,'240','05108','2026-11-01T09:00:00+09:00','신사역 방향')
        self.assertEqual(x.status,'PLANNED_SKIP')
        self.assertEqual(x.evidence[0]['pdf_page'],'17')
        y=check_impact(db,'240','05108','2026-11-01T12:00:00+09:00','신사역 방향')
        self.assertEqual(y.status,'NO_MATCHING_NOTICE')
        z=check_impact(db,'240','05108','2026-11-01T09:00:00+09:00',None)
        self.assertEqual(z.status,'DIRECTION_REQUIRED')

    def test_branch_requires_verified_pivot(self):
        db=db_fixture()
        root=candidate('R1','R1',45,'240','05108','2026-11-01T09:00:00+09:00')
        result=evaluate_candidates(db,[root],CatalogAlternativeProvider(),"global_fastest")
        self.assertEqual(result['decisions'][0]['outcome'],'NEEDS_REROUTE_PROVIDER')

    def test_branch_rechecked(self):
        db=db_fixture()
        root=candidate('R1','R1',45,'240','05108','2026-11-01T09:00:00+09:00')
        alt=candidate('R1a','R1',55,'147','11486','2026-11-01T09:02:00+09:00',
                      parent='R1',pivot='05108',calls=1)
        catalog=CatalogAlternativeProvider({'R1|05108':[candidate_to_dict(alt)]})
        result=evaluate_candidates(db,[root],catalog,'global_fastest')
        outcomes={x['candidate_id']:x['outcome'] for x in result['decisions']}
        self.assertEqual(outcomes['R1'],'BRANCHED')
        self.assertEqual(outcomes['R1a'],'NOTICE_COMPATIBLE')

    def test_60_boundary_and_61_rejection(self):
        db=db_fixture()
        root=candidate('R1','R1',45,'147','11486','2026-11-01T09:01:00+09:00')
        other=candidate('R2','R2',105,'147','11486','2026-11-01T09:01:00+09:00')
        third=candidate('R3','R3',106,'147','11486','2026-11-01T09:01:00+09:00')
        result=evaluate_candidates(db,[root,other,third],CatalogAlternativeProvider(),'global_fastest')
        out={d['candidate_id']:d['outcome'] for d in result['decisions']}
        self.assertEqual(out['R2'],'NOTICE_COMPATIBLE')
        self.assertEqual(out['R3'],'PRUNED_DURATION')

    def test_transfer_5_boundary_and_6_rejection(self):
        db=db_fixture()
        root=candidate('R1','R1',45,'147','11486','2026-11-01T09:01:00+09:00')
        five=candidate('R2','R2',45,'147','11486','2026-11-01T09:01:00+09:00',transfers=5)
        six=candidate('R3','R3',45,'147','11486','2026-11-01T09:01:00+09:00',transfers=6)
        result=evaluate_candidates(db,[root,five,six],CatalogAlternativeProvider(),'global_fastest')
        out={d['candidate_id']:d['outcome'] for d in result['decisions']}
        self.assertEqual(out['R2'],'NOTICE_COMPATIBLE')
        self.assertEqual(out['R3'],'PRUNED_TRANSFERS')

    def test_baseline_mode_is_required(self):
        db=db_fixture()
        root=candidate('R1','R1',45,'147','11486','2026-11-01T09:01:00+09:00')
        with self.assertRaises(ValueError):
            evaluate_candidates(db,[root],CatalogAlternativeProvider(),"")


def candidate_to_dict(c):
    return {"candidate_id":c.candidate_id,"root_id":c.root_id,
      "depart_at":c.depart_at,"arrive_at":c.arrive_at,
      "walk_distance_m":c.walk_distance_m,"parent_id":c.parent_id,
      "pivot_stop_id":c.pivot_stop_id,"reroute_calls":c.reroute_calls,
      "service_evidence":c.service_evidence,
      "visits":[{"stop_id":v.stop_id,"time":v.time,"route_name":v.route_name,
                 "direction":v.direction,"role":v.role} for v in c.visits]}

if __name__=='__main__':
    unittest.main()
