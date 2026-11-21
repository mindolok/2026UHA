"""Windows-friendly CLI for CityFlow offline data and route evaluation."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from .impact import check_impact
from .ingest import create_database
from .nearby import nearby_same_route
from .routing import evaluate_catalog_file
from .v1csv import load_active


def main(argv:list[str] | None=None) -> None:
    p=argparse.ArgumentParser(description="CityFlow offline event-aware bus routing")
    sub=p.add_subparsers(dest="command",required=True)
    build=sub.add_parser("build-db",help="Create SQLite database from CSV and XLSX")
    build.add_argument("--routes",required=True)
    build.add_argument("--stops",required=True)
    build.add_argument("--events",required=True)
    build.add_argument("--db",default="cityflow.db")
    q=sub.add_parser("check-stop",help="Check planned skipped stop from source notice")
    q.add_argument("--db",default="cityflow.db")
    q.add_argument("--route",required=True)
    q.add_argument("--stop",required=True)
    q.add_argument("--time",required=True,help="2026-11-01T09:00:00+09:00")
    q.add_argument("--direction",required=True,help="Exact text from event notice")
    n=sub.add_parser("nearby",help="Potential same-route stops inside straight-line radius")
    for a in (n,):
        a.add_argument("--db",default="cityflow.db")
        a.add_argument("--route",required=True)
        a.add_argument("--stop",required=True)
        a.add_argument("--time",required=True)
        a.add_argument("--direction",required=True)
        a.add_argument("--radius",type=float,default=300)
    e=sub.add_parser("evaluate",help="Evaluate baseline + provider-supplied branch candidates")
    e.add_argument("--db",default="cityflow.db")
    e.add_argument("--catalog",required=True)
    e.add_argument("--baseline-mode",choices=["global_fastest","per_root"],required=True,
                   help="Required team decision; no implicit choice")
    e.add_argument("--output",help="Optional output JSON")
    v=sub.add_parser("load-v1",help="Replace events with the team's v1 *_active.csv")
    v.add_argument("--db",default="cityflow.db")
    v.add_argument("--active",required=True)
    s=sub.add_parser("stats",help="Show imported data diagnostics")
    s.add_argument("--db",default="cityflow.db")
    args=p.parse_args(argv)
    if args.command=="build-db":
        result=create_database(args.routes,args.stops,args.events,args.db)
    elif args.command=="load-v1":
        result=load_active(args.db,args.active)
    else:
        db=sqlite3.connect(args.db)
        try:
            if args.command=="check-stop":
                result=check_impact(db,args.route,args.stop,args.time,args.direction).to_dict()
            elif args.command=="nearby":
                result=nearby_same_route(db,args.route,args.stop,args.time,args.direction,args.radius)
            elif args.command=="evaluate":
                result=evaluate_catalog_file(db,args.catalog,args.baseline_mode)
            elif args.command=="stats":
                result={
                    "route_stops":db.execute("SELECT count(*) FROM route_stops").fetchone()[0],
                    "routes":db.execute("SELECT count(*) FROM routes").fetchone()[0],
                    "stop_coordinates":db.execute("SELECT count(*) FROM stops").fetchone()[0],
                    "event_rows":db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "review_rows":db.execute("SELECT count(*) FROM events WHERE review_state<>'확인'").fetchone()[0],
                    "direction_not_original":db.execute("SELECT count(*) FROM events WHERE direction_basis<>'원문'").fetchone()[0],
                    "unmatched_route_stops":db.execute("SELECT count(*) FROM events WHERE route_stop_match=0").fetchone()[0]
                }
            else:raise AssertionError(args.command)
        finally:db.close()
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if args.command=="evaluate" and args.output:
        Path(args.output).write_text(output,encoding="utf-8")
    print(output)

if __name__=="__main__":
    main()
