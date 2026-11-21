import logging
import os
import time
import traceback
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine_adapter as engine

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

KAKAO_JS_KEY = os.getenv("KAKAO_JS_KEY", "")
KAKAO_REST_KEY = os.getenv("KAKAO_REST_KEY", "")

log = logging.getLogger("cityflow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

app = FastAPI(title="CITY FLOW API")

# 화면을 다른 주소에서 띄울 때만 사용 (예: CORS_ORIGINS=https://yeogita.vercel.app,http://localhost:5173)
_cors = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_and_errors(request: Request, call_next):
    """화면 파일이 예전 버전으로 캐시되지 않게 하고, 서버 오류는 내용과 함께 돌려줍니다."""
    t = time.time()
    try:
        resp = await call_next(request)
    except Exception as e:  # noqa: BLE001
        log.error("ERROR %s %s\n%s", request.method, request.url.path, traceback.format_exc())
        return JSONResponse({"detail": f"서버 오류: {type(e).__name__}: {e}"}, status_code=500)
    if request.url.path.startswith("/api/route"):
        log.info("route search %.2fs -> %s", time.time() - t, resp.status_code)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/config")
def config():
    # JS 키만 내려줍니다 (도메인 등록으로 보호되는 키). REST 키는 절대 내려주지 않음.
    return {"kakaoJsKey": KAKAO_JS_KEY, "engineReady": engine.ENGINE_READY,
            "searchReady": bool(KAKAO_REST_KEY), "event": engine.EVENT_LABEL}


async def _kakao(path: str, params: dict) -> dict:
    if not KAKAO_REST_KEY:
        raise HTTPException(503, "KAKAO_REST_KEY 가 설정되지 않았습니다 (.env)")
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"https://dapi.kakao.com{path}", params=params,
                        headers={"Authorization": f"KakaoAK {KAKAO_REST_KEY}"})
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"카카오 API 오류: {r.text[:200]}")
    return r.json()


@app.get("/api/search")
async def search(q: str = Query(min_length=1)):
    """카카오 로컬 키워드 검색 프록시 (REST 키는 서버에만). 서울시청 기준 가까운 순."""
    data = await _kakao("/v2/local/search/keyword.json",
                        {"query": q, "size": 8, "x": 126.978, "y": 37.5665, "radius": 20000})
    return [
        {"name": d["place_name"], "address": d["road_address_name"] or d["address_name"],
         "category": d.get("category_group_name", ""), "lat": float(d["y"]), "lng": float(d["x"])}
        for d in data.get("documents", [])
    ]


@app.get("/api/addr")
async def addr(lat: float, lng: float):
    """지도에서 고른 지점의 주소 (카카오 좌표→주소)."""
    try:
        data = await _kakao("/v2/local/geo/coord2address.json", {"x": lng, "y": lat})
        d = (data.get("documents") or [{}])[0]
        a = (d.get("road_address") or {}).get("address_name") or (d.get("address") or {}).get("address_name")
        return {"name": a or "지도에서 선택한 위치"}
    except HTTPException:
        return {"name": "지도에서 선택한 위치"}


@app.get("/api/controls")
def controls(time: str = "08:30"):
    return engine.controls(time)


@app.get("/api/stop")
def stop(route: str, ars: str, time: str = "08:30"):
    """시민 화면: 이 버스가 이 정류소에 서나? 안 서면 어디서 타나?"""
    return engine.stop(route, ars, time)


class Point(BaseModel):
    lat: float
    lng: float


class RouteReq(BaseModel):
    origin: Point
    dest: Point
    depart: str = "08:00"
    subway: bool = True


@app.post("/api/route")
def route(req: RouteReq):
    return engine.route(req.origin.model_dump(), req.dest.model_dump(), req.depart, req.subway)


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")
