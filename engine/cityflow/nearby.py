"""Spatial stop candidates. Distances are straight-line, NOT pedestrian routes."""
from __future__ import annotations
import math
import sqlite3
from .impact import check_impact
from .ingest import normalize_ars


def haversine_m(lng1:float,lat1:float,lng2:float,lat2:float) -> float:
    lat1,lat2=map(math.radians,(lat1,lat2))
    dlat=lat2-lat1;dlng=math.radians(lng2-lng1)
    a=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return 6371000*2*math.asin(min(1,math.sqrt(a)))


def nearby_same_route(db:sqlite3.Connection,route_name:str,from_stop_id:str,
                      time_iso:str,direction:str,radius_m:float=300) -> dict:
    ars=normalize_ars(from_stop_id)
    source=db.execute("""SELECT lng,lat FROM stops WHERE ars_id=? AND lng IS NOT NULL
                         AND lat IS NOT NULL LIMIT 1""",(ars,)).fetchone()
    if not source:
        return {"error":"Source stop has no coordinates; cannot calculate nearby candidates"}
    route_stops=db.execute("""SELECT DISTINCT rs.ars_id,rs.name,rs.lng,rs.lat
        FROM route_stops rs WHERE rs.route_name=? AND rs.lng IS NOT NULL
        AND rs.lat IS NOT NULL""",(route_name,)).fetchall()
    hints=[]
    for sid,name,lng,lat in route_stops:
        if sid==ars:continue
        dist=haversine_m(float(source[0]),float(source[1]),float(lng),float(lat))
        if dist>radius_m:continue
        impact=check_impact(db,route_name,sid,time_iso,direction)
        if impact.status in {"PLANNED_SKIP","POSSIBLE_SKIP_REVIEW","DIRECTION_REQUIRED"}:
            continue
        hints.append({"stop_id":sid,"name":name,"straight_line_m":round(dist,1),
                      "notice_match":impact.status,
                      "status":"POTENTIAL_ONLY_NOT_VERIFIED_AS_BOARDING_STOP"})
    hints.sort(key=lambda x:x["straight_line_m"])
    return {"reference_stop_id":ars,"search_radius_m":radius_m,
            "distance_is":"straight_line_not_pedestrian",
            "warning":"The route-stop master has no detour stop order, timetable, or accessible walking paths. Do not present these hints as confirmed alternatives.",
            "candidates":hints}


def goal_stops(db:sqlite3.Connection,lng:float,lat:float,radius_m:float=400) -> list[dict]:
    """Find geographical goal candidates (straight-line test only).

    A result cannot be considered an accessible walking route until confirmed
    by a pedestrian routing service or manual accessibility check.
    """
    result=[]
    for sid,name,x,y in db.execute("""SELECT ars_id,name,lng,lat FROM stops
                                   WHERE lng IS NOT NULL AND lat IS NOT NULL"""):
        d=haversine_m(lng,lat,float(x),float(y))
        if d<=radius_m:
            result.append({"stop_id":sid,"name":name,"straight_line_m":round(d,1),
                           "walk_route_verified":False})
    result.sort(key=lambda x:x["straight_line_m"])
    return result
