"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CTRL = { controlled: "#e53935", check: "#fb8c00" };

const S = {
  map: null, cfg: null, controls: null, showCtrl: false, focus: null,
  point: { origin: null, dest: null }, pins: {}, pick: null,
  ctrlLines: [], ctrlDots: [], drawn: [], popup: null, ctx: null, plans: [], searching: false, orig: null, origStatus: null,
};

// ---------------------------------------------------------------- helpers
async function api(url, opts = {}, timeoutMs = 90000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...opts, signal: ac.signal });
    const text = await r.text();
    let data; try { data = JSON.parse(text); } catch { data = null; }
    if (!r.ok) throw new Error((data && data.detail) || text || `HTTP ${r.status}`);
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답이 없습니다(90초). 서버가 잠에서 깨어나는 중일 수 있어요. 잠시 뒤 다시 눌러 주세요.");
    if (e instanceof TypeError) throw new Error("서버에 연결할 수 없습니다. uvicorn 서버가 켜져 있는지 확인하세요.");
    throw e;
  } finally { clearTimeout(timer); }
}

function status(kind, text) {
  const el = $("status");
  if (!kind) { el.innerHTML = ""; return; }
  el.innerHTML = kind === "loading"
    ? `<div class="loading"><span class="spin"></span>${esc(text)}</div>`
    : `<div class="err">${esc(text)}</div>`;
}

// Seoul bus colors (간선 blue, 지선 green, 광역 red, 순환/마을 yellow, 심야 navy)
const SUBWAY = { "1": "#0052A4", "2": "#00A84D", "3": "#EF7C1C", "4": "#00A5DE", "5": "#996CAC", "6": "#CD7C2F", "7": "#747F00", "8": "#E6186C" };
function busColor(name) {
  const n = String(name);
  const m = n.match(/^(\d)호선$/);
  if (m) return SUBWAY[m[1]] || "#555";
  if (/^N/.test(n) || n.includes("심야")) return "#1d3a6b";
  if (/^9\d{3}/.test(n) || n.includes("광역")) return "#e60012";
  if (/^6\d{3}$/.test(n) || n.includes("공항")) return "#9b7b3a";
  if (/^\d{4}$/.test(n)) return "#53b332";
  if (/^\d{3}$/.test(n)) return "#3d5bab";
  return "#e5a400";
}
const chip = (name) => `<span class="chip" style="background:${busColor(name)}">${esc(name)}</span>`;
const hhmmToKo = (t) => { const [h, m] = t.split(":").map(Number); return `${h < 12 ? "오전" : "오후"} ${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")}`; };

function loadKakao(key) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${key}&autoload=false`;
    s.onload = () => kakao.maps.load(resolve);
    s.onerror = () => reject(new Error("카카오 지도 로드 실패: JS 키와 [JavaScript SDK 도메인] 등록을 확인하세요."));
    document.head.appendChild(s);
  });
}
const LL = (p) => new kakao.maps.LatLng(p[0], p[1]);
const overlay = (lat, lng, html, opt = {}) => {
  const o = new kakao.maps.CustomOverlay({ position: new kakao.maps.LatLng(lat, lng), content: html, ...opt });
  o.setMap(S.map); return o;
};
const clear = (arr) => { arr.forEach((o) => o.setMap(null)); arr.length = 0; };

// ---------------------------------------------------------------- origin / destination
function setPoint(kind, p, name) {
  S.point[kind] = { lat: p.lat, lng: p.lng, name };
  $(kind === "origin" ? "inOrigin" : "inDest").value = name;
  if (S.pins[kind]) S.pins[kind].setMap(null);
  const c = kind === "origin" ? "#1fa53d" : "#e53935";
  S.pins[kind] = overlay(p.lat, p.lng,
    `<div class="pin" style="--c:${c}"><div style="background:${c}">${kind === "origin" ? "출발" : "도착"}</div></div>`,
    { yAnchor: 1, zIndex: 10 });
  if (S.point.origin && S.point.dest) searchRoute();
  else S.map.panTo(new kakao.maps.LatLng(p.lat, p.lng));
}

async function setPointFromMap(kind, latLng) {
  const p = { lat: latLng.getLat(), lng: latLng.getLng() };
  let name = "지도에서 선택한 위치";
  if (S.cfg.searchReady) { try { name = (await api(`/api/addr?lat=${p.lat}&lng=${p.lng}`, {}, 5000)).name; } catch {} }
  setPoint(kind, p, name);
}

function autocomplete(kind) {
  const input = $(kind === "origin" ? "inOrigin" : "inDest");
  const list = $(kind === "origin" ? "acOrigin" : "acDest");
  let timer = null, items = [], sel = -1;
  const close = () => { list.classList.remove("show"); sel = -1; };
  const choose = (i) => { const it = items[i]; if (!it) return; close(); setPoint(kind, it, it.name); };
  const render = () => {
    list.innerHTML = items.map((it, i) =>
      `<li data-i="${i}" class="${i === sel ? "sel" : ""}"><b>${esc(it.name)}</b><small>${esc(it.address)}${it.category ? " · " + esc(it.category) : ""}</small></li>`).join("");
    list.classList.toggle("show", items.length > 0);
  };
  input.addEventListener("input", () => {
    S.point[kind] = null;
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { items = []; close(); return; }
    if (!S.cfg.searchReady) { status("err", "장소 검색에는 .env 의 KAKAO_REST_KEY 가 필요합니다. 지도를 우클릭해 지정해 주세요."); return; }
    timer = setTimeout(async () => {
      try { items = await api(`/api/search?q=${encodeURIComponent(q)}`, {}, 6000); sel = -1; render(); }
      catch (e) { status("err", e.message); }
    }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (!list.classList.contains("show")) return;
    if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter") { choose(sel < 0 ? 0 : sel); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  list.addEventListener("mousedown", (e) => { const li = e.target.closest("li"); if (li) { e.preventDefault(); choose(+li.dataset.i); } });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

function showContextMenu(latLng) {
  if (S.ctx) S.ctx.setMap(null);
  const el = document.createElement("div");
  el.className = "ctx";
  el.innerHTML = `<button data-k="origin">🟢 출발지로 설정</button><button data-k="dest">🔴 도착지로 설정</button>`;
  el.onclick = (e) => { const k = e.target.dataset.k; if (!k) return; S.ctx.setMap(null); S.ctx = null; setPointFromMap(k, latLng); };
  S.ctx = new kakao.maps.CustomOverlay({ position: latLng, content: el, xAnchor: 0, yAnchor: 0, zIndex: 30 });
  S.ctx.setMap(S.map);
}

// ---------------------------------------------------------------- control layer
function popup(lat, lng, html) {
  if (S.popup) S.popup.setMap(null);
  const el = document.createElement("div");
  el.className = "pop";
  el.innerHTML = `<button class="x">✕</button>${html}`;
  el.querySelector(".x").onclick = () => { S.popup.setMap(null); S.popup = null; };
  S.popup = new kakao.maps.CustomOverlay({ position: new kakao.maps.LatLng(lat, lng), content: el, yAnchor: 1, zIndex: 40 });
  S.popup.setMap(S.map);
}

// show all controls only when the toggle is on; otherwise only for the routes in focus
const ctrlVisible = (route) => S.showCtrl || (S.focus && S.focus.has(route));

function drawCtrlDots() {
  clear(S.ctrlDots);
  if (!S.controls || (!S.showCtrl && !S.focus)) return;
  if (S.map.getLevel() > (S.showCtrl ? 5 : 8)) return;
  const b = S.map.getBounds();
  const seen = new Set();
  let n = 0;
  for (const s of S.controls.stops) {
    const key = s.id + s.status;
    if (!ctrlVisible(s.route) || seen.has(key) || !b.contain(new kakao.maps.LatLng(s.lat, s.lng))) continue;
    seen.add(key);
    const el = document.createElement("div");
    el.style.cssText = `width:11px;height:11px;border-radius:50%;background:${CTRL[s.status]};border:2px solid #fff;box-shadow:0 0 2px rgba(0,0,0,.6);cursor:pointer`;
    el.onclick = () => {
      const routes = S.controls.stops.filter((x) => x.id === s.id && ctrlVisible(x.route));
      popup(s.lat, s.lng, `<b>${esc(s.name)}</b> <span style="color:#888">${esc(s.id)}</span><br>` +
        routes.map((x) => `${chip(x.route)} ${x.status === "check" ? "확인 필요" : "무정차"} ~${x.until} <span style="color:#999">공지 ${esc(x.pdf_page)}쪽</span>`).join("<br>"));
    };
    S.ctrlDots.push(overlay(s.lat, s.lng, el, { xAnchor: 0.5, yAnchor: 0.5, zIndex: 3 }));
    if (++n >= 600) break;
  }
}

function renderCtrl() {
  clear(S.ctrlLines);
  if (S.controls) {
    for (const g of S.controls.segments) {
      if (!ctrlVisible(g.route)) continue;
      const l = new kakao.maps.Polyline({ path: g.path.map(LL), strokeWeight: 5, strokeColor: "#e53935",
                                         strokeOpacity: S.showCtrl ? 0.3 : 0.55, zIndex: 2 });
      l.setMap(S.map); S.ctrlLines.push(l);
    }
  }
  drawCtrlDots();
  $("legend").hidden = !S.showCtrl && !(S.focus && S.ctrlLines.length);
}

function setFocus(routes) { S.focus = routes && routes.length ? new Set(routes) : null; renderCtrl(); }

async function loadControls() {
  S.controls = await api(`/api/controls?time=${$("time").value}`);
  const cnt = new Set(S.controls.stops.map((s) => s.id)).size;
  $("event").textContent = `${S.controls.event} · ${S.controls.time} 기준 무정차 정류장 ${cnt}곳`;
  renderCtrl();
}

// ---------------------------------------------------------------- route search
async function searchRoute() {
  if (S.searching) { S.pending = true; return; }   // run again with the latest points when done
  if (!S.point.origin || !S.point.dest) { status("err", "출발지와 도착지를 정해 주세요. (검색하거나 지도를 우클릭)"); return; }
  S.searching = true; $("btnRoute").disabled = true;
  $("results").innerHTML = ""; clear(S.drawn); setFocus(null);
  status("loading", "통제 정보를 반영해 경로를 찾는 중… (처음 요청은 최대 1분 걸릴 수 있어요)");
  try {
    const res = await api("/api/route", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: S.point.origin, dest: S.point.dest, depart: $("time").value, subway: $("useSubway").checked }),
    });
    status(); renderRoute(res);
  } catch (e) { status("err", e.message); }
  finally {
    S.searching = false; $("btnRoute").disabled = false;
    if (S.pending) { S.pending = false; searchRoute(); }
  }
}

const sign = (n, unit) => (n > 0 ? `+${n}${unit}` : n < 0 ? `−${Math.abs(n)}${unit}` : `±0${unit}`);
const ROLE = { BOARD: "승차", TRANSFER_BOARD: "환승 승차", ALIGHT: "하차", TRANSFER_ALIGHT: "환승 하차", VIA: "경유" };

function compareLine(c) {
  if (!c) return "";
  if (c.same_as_original) return `<div class="cmp same">평소와 같은 경로</div>`;
  const cls = c.d_minutes > 0 ? "up" : c.d_minutes < 0 ? "down" : "";
  let h = `<div class="cmp">평소 대비 <b class="${cls}">${sign(c.d_minutes, "분")}</b>`;
  if (c.d_transfers) h += ` · 환승 ${sign(c.d_transfers, "회")}`;
  if (Math.abs(c.d_walk_m) >= 50) h += ` · 도보 ${sign(c.d_walk_m, "m")}`;
  h += `</div>`;
  const parts = [];
  if (c.buses_removed.length) parts.push(`<span class="lbl">빠진 노선</span> ${c.buses_removed.map(chip).join(" ")}`);
  if (c.buses_added.length) parts.push(`<span class="lbl">새 노선</span> ${c.buses_added.map(chip).join(" ")}`);
  if (!c.buses_removed.length && !c.buses_added.length) {
    if (c.first_board) parts.push(`<span class="lbl">타는 곳 변경</span> ${esc(c.first_board)}`);
    if (c.last_alight) parts.push(`<span class="lbl">내리는 곳 변경</span> ${esc(c.last_alight)}`);
  }
  if (parts.length) h += `<div class="cmp2">${parts.join("<br>")}</div>`;
  return h;
}

function compareTable(p) {
  const o = S.orig, c = p.compare;
  if (!o || !c || c.same_as_original) return "";
  const busList = (q) => q.legs.filter((l) => l.mode === "bus").map((l) => chip(l.route)).join(" ") || "도보";
  const firstBoard = (q) => (q.legs.find((l) => l.mode === "bus") || {}).from_name || "-";
  const lastAlight = (q) => ([...q.legs].reverse().find((l) => l.mode === "bus") || {}).to_name || "-";
  const ch = o.check;
  const origEff = ch.status === "OK" ? "영향 없음"
    : ch.impacts.map((i) => `<span class="bad">${esc(i.stop)} ${ROLE[i.role]} 불가</span>`).filter((_, k) => ch.impacts[k].role !== "VIA").join("<br>")
      + (ch.impacts.some((i) => i.role === "VIA") ? `${ch.impacts.some((i) => i.role !== "VIA") ? "<br>" : ""}<span class="warnc">무정차 구간 ${ch.impacts.filter((i) => i.role === "VIA").length}곳 통과</span>` : "");
  const pass = p.legs.reduce((n, l) => n + (l.passed_blocks || []).length, 0);
  const newEff = (c.avoided.length ? `<span class="good">무정차 ${c.avoided.length}곳 피함</span>` : "")
    + (pass ? `${c.avoided.length ? "<br>" : ""}<span class="warnc">무정차 구간 ${pass}곳 통과(+${pass * 10}분 추정)</span>` : "")
    || "영향 없음";
  const row = (k, a, b, diff) => `<tr${diff ? ' class="diff"' : ""}><th>${k}</th><td>${a}</td><td>${b}</td></tr>`;
  const om = ch.minutes_with_events != null && ch.minutes_with_events !== o.minutes
    ? `${o.minutes}분<br><small>통제 시 약 ${ch.minutes_with_events}분</small>` : `${o.minutes}분${ch.status === "BLOCKED" ? "<br><small>통제 시 이용 불가</small>" : ""}`;
  return `<table class="cmpt"><thead><tr><th></th><th>평소 경로</th><th>이 경로</th></tr></thead><tbody>
    ${row("소요", om, `<b>${p.minutes}분</b>`, c.d_minutes !== 0)}
    ${row("도착", o.arrive, p.arrive, o.arrive !== p.arrive)}
    ${row("환승", `${o.transfers}회`, `${p.transfers}회`, c.d_transfers !== 0)}
    ${row("도보", `${o.walk_m}m`, `${p.walk_m}m`, Math.abs(c.d_walk_m) >= 50)}
    ${row("노선", busList(o), busList(p), c.buses_added.length || c.buses_removed.length)}
    ${row("타는 곳", esc(firstBoard(o)), esc(firstBoard(p)), firstBoard(o) !== firstBoard(p))}
    ${row("내리는 곳", esc(lastAlight(o)), esc(lastAlight(p)), lastAlight(o) !== lastAlight(p))}
    ${row("통제 영향", origEff, newEff, true)}
  </tbody></table>`;
}

function planHTML(p, idx, check, avoid) {
  const total = Math.max(p.minutes, 1);
  const segs = [];
  let t = null;
  const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
  for (const l of p.legs) {
    const d = toMin(l.depart), a = toMin(l.arrive);
    if (t != null && d - t >= 1) segs.push({ cls: "wait", m: d - t, label: "" });
    const m = Math.max(a - d, 0);
    segs.push(l.mode === "bus" ? { bus: l.route, m, label: `${l.route} ${m}분` } : { cls: "w", m, label: m >= 4 ? `${m}분` : "" });
    t = a;
  }
  const bar = segs.map((s) => `<div class="${s.cls || ""}" style="flex:${Math.max(s.m, 0.5)};${s.bus ? "background:" + busColor(s.bus) : ""}">${esc(s.label)}</div>`).join("");
  const buses = p.legs.filter((l) => l.mode === "bus");
  const walkMin = p.legs.filter((l) => l.mode === "walk").reduce((s, l) => s + (toMin(l.arrive) - toMin(l.depart)), 0);
  const chips = buses.map((l, i) => `${i ? "→ " : ""}${chip(l.route)} <span class="at">${esc(l.from_name)}</span>`).join(" ");
  return `<div class="plan" data-i="${idx}">
    <div class="top"><span class="min">${p.minutes >= 60 ? `${Math.floor(p.minutes / 60)}<small>시간</small> ${p.minutes % 60}` : p.minutes}<small>분</small></span>
      <span class="arr">${hhmmToKo(p.arrive)} 도착</span>
      ${check ? '<span class="tag chk">확인 필요</span>' : avoid && !(p.compare && p.compare.same_as_original) ? '<span class="tag avoid">통제 회피</span>' : ""}</div>
    <div class="meta">${buses.length ? `환승 ${p.transfers}회 · ` : ""}도보 ${walkMin}분(${p.walk_m}m)</div>
    ${S.origStatus && S.origStatus !== "OK" ? compareLine(p.compare) : ""}
    <div class="bar">${bar}</div>
    <div class="chips">${chips || "도보로 이동"}</div>
    ${p.reasons.length ? `<div class="reason">⚠ ${p.reasons.map(esc).join(" / ")}</div>` : ""}
    <div class="detail"></div></div>`;
}

function stepsHTML(p) {
  const out = [`<li style="--c:#1fa53d"><span class="t">${p.depart}</span><b>출발</b> ${esc(S.point.origin?.name || "")}</li>`];
  for (const l of p.legs) {
    if (l.mode === "walk") {
      out.push(`<li style="--c:#bbb"><span class="t">${l.depart}</span>도보 ${Math.round(l.distance_m)}m
        <div class="sub">${esc(l.to_name === "도착지" ? "도착지까지" : l.to_name + "까지")}</div></li>`);
    } else {
      const c = busColor(l.route);
      const skip = l.passed_blocks.length
        ? `<div class="skip">⚠ 무정차 구간 통과(${l.passed_blocks.map((b) => esc(b.stop_name)).join(", ")}) · 우회 지연 약 ${10 * l.passed_blocks.length}분 포함(추정)</div>` : "";
      out.push(`<li style="--c:${c}"><span class="t">${l.depart}</span>${chip(l.route)} <b>${esc(l.from_name)}</b> 승차
        <div class="sub">${l.path.length - 1}개 ${l.kind === "subway" ? "역" : "정류장"} 이동${l.kind === "subway" ? " · 지하철은 통제 영향 없음" : ""}</div>${skip}</li>`);
      out.push(`<li style="--c:${c}"><span class="t">${l.arrive}</span><b>${esc(l.to_name)}</b> 하차</li>`);
    }
  }
  out.push(`<li style="--c:#e53935"><span class="t">${p.arrive}</span><b>도착</b> ${esc(S.point.dest?.name || "")}</li>`);
  return `<ul class="steps">${out.join("")}</ul>`;
}

function origHTML(o, status) {
  const ch = o.check;
  const head = {
    BLOCKED: `<b>평소 경로는 오늘 이 시간에 이용할 수 없어요.</b>`,
    DELAYED: `<b>평소 경로가 무정차 구간을 지나 약 ${ch.minutes_with_events - o.minutes}분 늦어져요.</b>`,
    CHECK: `<b>평소 경로에 통제 여부를 확인해야 하는 구간이 있어요.</b>`,
    OK: `<b>이 시간엔 평소 경로를 그대로 이용할 수 있어요.</b>`,
  }[status];
  const imp = ch.impacts.map((i) =>
    `<li>${chip(i.route)} <b>${esc(i.stop)}</b> ${ROLE[i.role]} · ${esc(i.effect)} <span class="t">${i.time} 기준, ~${i.until}${i.state === "CHECK" ? " · 확인 필요" : ""}</span></li>`).join("");
  return `<div class="banner ${status === "OK" ? "good" : "bad"}">${head}
    <div class="origline">평소 경로: ${o.legs.filter((l) => l.mode === "bus").map((l) => chip(l.route)).join(" → ") || "도보"} · ${o.minutes}분 · ${hhmmToKo(o.arrive)} 도착
      ${status !== "OK" ? `<button class="link" id="showOrig">지도에서 보기</button>` : ""}</div>
    ${imp ? `<ul class="imp">${imp}</ul>` : ""}</div>`;
}

function renderRoute(res) {
  const box = $("results");
  if (res.t0_min == null) { box.innerHTML = `<div class="banner bad">${esc(res.message)}</div>`; return; }
  S.orig = res.original[0]; S.origStatus = res.original_status;
  let html = origHTML(S.orig, res.original_status);
  S.plans = [];
  if (res.confirmed.length) {
    html += `<div class="group">추천 경로 ${res.confirmed.length}개 <span class="sub">평소 ${res.t0_min}분 + 60분 이내 · 환승 5회 이하 · 파레토 순위</span></div>`;
    for (const p of res.confirmed) { S.plans.push(p); html += planHTML(p, S.plans.length - 1, false, res.original_affected); }
  }
  if (res.needs_check.length) {
    html += `<div class="group warn">더 빠르지만 통제 확인이 필요한 경로</div>`;
    for (const p of res.needs_check) { S.plans.push(p); html += planHTML(p, S.plans.length - 1, true, false); }
  }
  if (!S.plans.length) html += `<div class="banner bad">평소보다 60분 이내로 갈 수 있는 경로를 찾지 못했습니다.</div>`;
  const a = res.assumptions;
  html += `<div class="foot">소요시간은 추정값입니다. 노선 자료에 시간표가 없어 평균 ${a.bus_kmh}km/h, 승차 대기 ${a.wait_s / 60}분, 무정차 구간 통과 시 +10분으로 계산했습니다.${res.subway ? " 지하철(1~8호선)은 서울교통공사 역간 표준 운행시간에 대기 4분·출구 이동 2분을 더했고, 교통통제 영향이 없다고 봅니다." : " 지하철 제외(버스만) 결과입니다."} 통제 정보: ${esc(res.event)}</div>`;
  box.innerHTML = html;
  box.querySelectorAll(".plan").forEach((el) => (el.onclick = () => selectPlan(+el.dataset.i)));
  const so = $("showOrig");
  if (so) so.onclick = () => drawOriginal(true);
  if (S.plans.length) selectPlan(0);
}

// original route in grey + red marks where the controls hit it
function drawOriginal(fit) {
  const o = S.orig;
  if (!o || S.origStatus === "OK") return;
  const bounds = new kakao.maps.LatLngBounds();
  for (const l of o.legs) {
    const path = l.path.map(LL);
    path.forEach((x) => bounds.extend(x));
    const line = new kakao.maps.Polyline({ path, strokeWeight: l.mode === "bus" ? 6 : 3, strokeColor: "#8a8a8a",
      strokeStyle: l.mode === "bus" ? "shortdash" : "shortdot", strokeOpacity: 0.8, zIndex: 3 });
    line.setMap(S.map); S.drawn.push(line);
  }
  for (const i of o.check.impacts) {
    const leg = o.legs.find((l) => l.mode === "bus" && l.route === i.route);
    if (!leg) continue;
    const k = i.seq - leg.board_seq;
    const pt = leg.path[Math.max(0, Math.min(k, leg.path.length - 1))];
    S.drawn.push(overlay(pt[0], pt[1], `<div class="xmark" title="${esc(i.stop)}">✕ ${esc(i.stop)} ${i.role === "VIA" ? "무정차" : ROLE[i.role] + " 불가"}</div>`, { yAnchor: 1.3, zIndex: 7 }));
  }
  if (fit) S.map.setBounds(bounds, 60, 40, 60, 40);
}

function selectPlan(i) {
  const p = S.plans[i];
  document.querySelectorAll(".plan").forEach((el) => {
    const on = +el.dataset.i === i;
    el.classList.toggle("sel", on);
    el.querySelector(".detail").innerHTML = on ? compareTable(p) + stepsHTML(p) : "";
  });
  clear(S.drawn);
  if (p.compare && !p.compare.same_as_original) drawOriginal(false);
  setFocus(p.legs.filter((l) => l.mode === "bus").map((l) => l.route));
  const bounds = new kakao.maps.LatLngBounds();
  for (const l of p.legs) {
    const path = l.path.map(LL);
    path.forEach((x) => bounds.extend(x));
    if (l.mode === "bus") {
      const c = busColor(l.route);
      const under = new kakao.maps.Polyline({ path, strokeWeight: 10, strokeColor: "#fff", strokeOpacity: 1, zIndex: 4 });
      const line = new kakao.maps.Polyline({ path, strokeWeight: 6, strokeColor: c, strokeOpacity: 1, zIndex: 5 });
      under.setMap(S.map); line.setMap(S.map); S.drawn.push(under, line);
      const [a, z] = [l.path[0], l.path[l.path.length - 1]];
      S.drawn.push(overlay(a[0], a[1], `<div class="stoplbl" style="--c:${c}">${esc(l.route)} 승차 · ${esc(l.from_name)}</div>`, { yAnchor: 1.6, zIndex: 8 }));
      S.drawn.push(overlay(z[0], z[1], `<div class="stoplbl" style="--c:${c}">하차 · ${esc(l.to_name)}</div>`, { yAnchor: 1.6, zIndex: 8 }));
    } else {
      const line = new kakao.maps.Polyline({ path, strokeWeight: 4, strokeColor: "#666", strokeStyle: "shortdot", strokeOpacity: 0.9, zIndex: 5 });
      line.setMap(S.map); S.drawn.push(line);
    }
  }
  S.map.setBounds(bounds, 60, 40, 60, 40);
}

// ---------------------------------------------------------------- bus tab
async function checkStop() {
  const bus = $("busNo").value.trim(), ars = $("arsNo").value.trim();
  if (!bus || !ars) { status("err", "버스 번호와 정류소 번호를 입력하세요."); return; }
  $("btnStop").disabled = true; status("loading", "확인 중…");
  try {
    const r = await api(`/api/stop?route=${encodeURIComponent(bus)}&ars=${encodeURIComponent(ars)}&time=${$("time2").value}`);
    status(); renderStop(r);
  } catch (e) { status("err", e.message); }
  finally { $("btnStop").disabled = false; }
}

function renderStop(r) {
  clear(S.drawn);
  setFocus(r.visits ? [r.route] : null);
  const box = $("results");
  if (!r.visits) { box.innerHTML = `<div class="banner bad">${esc(r.message)}</div>`; return; }
  const cls = { NORMAL: "s-ok", CONTROLLED: "s-no", CHECK: "s-chk" }, word = { NORMAL: "정상 정차", CONTROLLED: "무정차", CHECK: "확인 필요" };
  let html = `<div class="stopres"><div class="head">${chip(r.route)} ${esc(r.visits[0].name)}<span class="state ${cls[r.status]}">${word[r.status]}</span></div>
    <div style="margin-top:6px">${esc(r.message)}</div>`;
  const bounds = new kakao.maps.LatLngBounds();
  for (const v of r.visits) {
    bounds.extend(new kakao.maps.LatLng(v.lat, v.lng));
    const c = v.state === "NORMAL" ? "#1fa53d" : v.state === "CHECK" ? "#fb8c00" : "#e53935";
    S.drawn.push(overlay(v.lat, v.lng, `<div class="stoplbl" style="--c:${c}">${esc(v.name)} · ${word[v.state] || ""}</div>`, { yAnchor: 1.4, zIndex: 9 }));
    html += `<div class="dir"><div class="tw">${esc(v.toward)} 방향 <span class="state ${cls[v.state] || "s-ok"}">${word[v.state] || "정상"}</span></div>`;
    for (const w of v.windows) html += `<div class="hint">${w.start}~${w.end}${w.end_estimated ? "(추정)" : ""} · ${esc(w.direction)} · 공지 ${esc(w.pdf_page)}쪽</div>`;
    for (const a of v.alternatives) {
      html += `<div class="alt"><b>대신 여기서 타세요</b> · ${esc(a.kind)}<br>${esc(a.name)} (${esc(a.ars)}) · 도보 약 ${a.walk_m}m, ${a.walk_min}분</div>`;
      bounds.extend(new kakao.maps.LatLng(a.lat, a.lng));
      S.drawn.push(overlay(a.lat, a.lng, `<div class="stoplbl" style="--c:#1fa53d">여기서 타세요 · ${esc(a.name)}</div>`, { yAnchor: 1.4, zIndex: 9 }));
      const l = new kakao.maps.Polyline({ path: [LL([v.lat, v.lng]), LL([a.lat, a.lng])], strokeWeight: 4, strokeColor: "#1fa53d", strokeStyle: "shortdot", zIndex: 5 });
      l.setMap(S.map); S.drawn.push(l);
    }
    html += `</div>`;
  }
  if (r.uncertain_notices && r.uncertain_notices.length) html += `<p class="reason">⚠ 이 정류장 이름으로 방향이 불확실한 무정차 공지가 있습니다(${r.uncertain_notices.map((u) => `${esc(u.direction)} ${u.start}~${u.end}, 공지 ${esc(u.pdf_page)}쪽`).join(" / ")}). 어느 쪽 정류장인지 확정되지 않아 판정에는 쓰지 않았습니다. 현장 안내를 확인하세요.</p>`;
  if (r.route_warnings.length) html += `<p class="reason">⚠ 정류소가 특정되지 않은 통제 공지: ${r.route_warnings.map((w) => esc(w.note)).join(" / ")}</p>`;
  html += `<p class="hint">도보 거리는 직선거리 기준 추정입니다. 통제 정보: ${esc(r.event)}</p></div>`;
  box.innerHTML = html;
  S.map.setBounds(bounds, 80, 60, 80, 60);
  if (S.map.getLevel() < 3) S.map.setLevel(3);
}

// demo inputs (2026-11-01 JTBC marathon)
const EXAMPLES = [
  [{ lat: 37.5259231137, lng: 126.9243582037, name: "여의도환승센터" }, { lat: 37.576162, lng: 126.971415, name: "경복궁역" }, "08:00"],
  [{ lat: 37.573648, lng: 126.976997, name: "세종문화회관" }, { lat: 37.535233709, lng: 127.0689474032, name: "건대입구(신양초)" }, "10:00"],
  [{ lat: 37.5502676468, lng: 127.0077186988, name: "송도병원(신당)" }, { lat: 37.485411, lng: 127.03885, name: "양재역" }, "09:00"],
];

// ---------------------------------------------------------------- tabs & boot
function tab(which) {
  $("tabRoute").classList.toggle("on", which === "route");
  $("tabBus").classList.toggle("on", which === "bus");
  $("paneRoute").hidden = which !== "route";
  $("paneBus").hidden = which !== "bus";
  $("results").innerHTML = ""; status(); clear(S.drawn); setFocus(null);
}

async function main() {
  S.cfg = await api("/api/config");
  if (!S.cfg.kakaoJsKey) { status("err", ".env 에 KAKAO_JS_KEY 를 설정한 뒤 서버를 다시 켜 주세요."); return; }
  await loadKakao(S.cfg.kakaoJsKey);
  S.map = new kakao.maps.Map($("map"), { center: new kakao.maps.LatLng(37.5665, 126.978), level: 6 });
  S.map.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.RIGHT);
  S.map.addControl(new kakao.maps.MapTypeControl(), kakao.maps.ControlPosition.TOPRIGHT);
  if (!S.cfg.searchReady) $("hint").innerHTML = "장소 검색은 KAKAO_REST_KEY 가 있어야 돼요. 지금은 지도를 <b>우클릭</b>해 출발·도착을 정해 주세요.";

  kakao.maps.event.addListener(S.map, "idle", drawCtrlDots);
  kakao.maps.event.addListener(S.map, "rightclick", (e) => showContextMenu(e.latLng));
  kakao.maps.event.addListener(S.map, "click", (e) => {
    if (S.ctx) { S.ctx.setMap(null); S.ctx = null; }
    if (S.pick) { const k = S.pick; S.pick = null; $("mapOrigin").classList.remove("on"); $("mapDest").classList.remove("on"); status(); setPointFromMap(k, e.latLng); }
  });

  autocomplete("origin"); autocomplete("dest");
  for (const [btn, k] of [["mapOrigin", "origin"], ["mapDest", "dest"]]) {
    $(btn).onclick = () => { S.pick = k; $(btn).classList.add("on"); status("loading", `지도에서 ${k === "origin" ? "출발지" : "도착지"}를 클릭하세요`); };
  }
  $("swap").onclick = () => {
    const { origin, dest } = S.point;
    S.point = { origin: null, dest: null };
    Object.values(S.pins).forEach((p) => p && p.setMap(null)); S.pins = {};
    $("inOrigin").value = ""; $("inDest").value = "";
    if (dest) setPoint("origin", dest, dest.name);
    if (origin) setPoint("dest", origin, origin.name);
  };
  $("btnRoute").onclick = searchRoute;
  $("useSubway").onchange = () => { if (S.point.origin && S.point.dest) searchRoute(); };
  document.querySelectorAll(".ex button").forEach((b) => (b.onclick = () => {
    const [o, d, t] = EXAMPLES[+b.dataset.i];
    $("time").value = t; $("time2").value = t;
    S.point = { origin: null, dest: null };
    loadControls().catch(() => {});
    setPoint("origin", o, o.name); setPoint("dest", d, d.name);
  }));
  $("btnStop").onclick = checkStop;
  $("arsNo").addEventListener("keydown", (e) => e.key === "Enter" && checkStop());
  $("tabRoute").onclick = () => tab("route");
  $("tabBus").onclick = () => tab("bus");
  $("time").onchange = () => { $("time2").value = $("time").value; loadControls().catch((e) => status("err", e.message)); if (S.point.origin && S.point.dest) searchRoute(); };
  $("time2").onchange = () => { $("time").value = $("time2").value; loadControls().catch((e) => status("err", e.message)); };
  $("toggleCtrl").onclick = () => {
    S.showCtrl = !S.showCtrl; $("toggleCtrl").classList.toggle("on", S.showCtrl);
    $("toggleCtrl").textContent = S.showCtrl ? "교통통제 전체 끄기" : "교통통제 전체 보기";
    renderCtrl();
  };

  await loadControls();
}

main().catch((e) => status("err", e.message));
