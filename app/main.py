import os
import json
import time
import random
import hashlib
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

app = FastAPI(title="KatMo Trends Collector", version="2.0.0")

API_KEY = os.getenv("KATMO_API_KEY", "").strip()

# Conservative defaults for Render Free + Google Trends.
REQUEST_GAP_SECONDS = float(os.getenv("TRENDS_REQUEST_GAP_SECONDS", "3.5"))
RETRY_BASE_SECONDS = float(os.getenv("TRENDS_RETRY_BASE_SECONDS", "8"))
MAX_RETRIES = int(os.getenv("TRENDS_MAX_RETRIES", "3"))
CACHE_TTL_SECONDS = int(os.getenv("TRENDS_CACHE_TTL_SECONDS", "21600"))  # 6h

_cache: Dict[str, Dict[str, Any]] = {}
_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_last_request_at = 0.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class CandidateRequest(BaseModel):
    candidate_id: str
    topic: str
    queries: List[str] = Field(min_length=1, max_length=5)
    geo: str = "US"
    include_five_year: bool = True


def check_auth(authorization: Optional[str]):
    if not API_KEY:
        return
    expected = f"Bearer {API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _cache_key(payload: CandidateRequest) -> str:
    raw = json.dumps(
        {
            "topic": payload.topic,
            "queries": payload.queries,
            "geo": payload.geo,
            "include_five_year": payload.include_five_year,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def ensure_browser():
    global _pw, _browser, _context, _page
    if _browser is not None and _context is not None and _page is not None:
        return
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True)
    _context = await _browser.new_context(
        user_agent=UA,
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": 1365, "height": 900},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        },
    )
    _page = await _context.new_page()
    # Warm Google domain and establish cookies before API calls.
    try:
        await _page.goto(
            "https://trends.google.com/trends/explore?geo=US",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await _page.wait_for_timeout(2500)
    except Exception:
        pass


async def paced_wait():
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if elapsed < REQUEST_GAP_SECONDS:
        await _page.wait_for_timeout(int((REQUEST_GAP_SECONDS - elapsed) * 1000))
    # Jitter reduces deterministic bursts.
    await _page.wait_for_timeout(random.randint(600, 1400))
    _last_request_at = time.monotonic()


async def browser_fetch_json(url: str):
    await ensure_browser()
    await paced_wait()
    result = await _page.evaluate(
        """async (url) => {
            const r = await fetch(url, {
              credentials: 'include',
              headers: {
                'accept': 'application/json, text/plain, */*',
                'x-client-data': ''
              }
            });
            const text = await r.text();
            return {status: r.status, text};
        }""",
        url,
    )
    return result["status"], result["text"]


def strip_xssi(text: str) -> str:
    if text.startswith(")]}',"):
        return text.split("\n", 1)[1] if "\n" in text else text[5:]
    return text


async def fetch_with_backoff(url: str):
    last_status = None
    last_text = ""
    for attempt in range(MAX_RETRIES + 1):
        status, text = await browser_fetch_json(url)
        last_status, last_text = status, text

        if status == 200:
            return status, text, attempt

        if status == 429:
            if attempt < MAX_RETRIES:
                # Exponential backoff + jitter: 8s, 16s, 32s by default.
                sleep_s = RETRY_BASE_SECONDS * (2 ** attempt) + random.uniform(1.0, 4.0)
                await _page.wait_for_timeout(int(sleep_s * 1000))
                # Refresh normal Trends page to renew cookies/session.
                try:
                    await _page.goto(
                        "https://trends.google.com/trends/explore?geo=US",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await _page.wait_for_timeout(random.randint(1800, 3200))
                except Exception:
                    pass
                continue
        break

    return last_status, last_text, MAX_RETRIES


def build_explore_url(queries: List[str], geo: str, timeframe: str) -> str:
    import urllib.parse
    comparison = [
        {"keyword": q, "geo": geo, "time": timeframe}
        for q in queries
    ]
    req = {
        "comparisonItem": comparison,
        "category": 0,
        "property": "",
    }
    params = {
        "hl": "en-US",
        "tz": "240",
        "req": json.dumps(req, separators=(",", ":")),
    }
    return "https://trends.google.com/trends/api/explore?" + urllib.parse.urlencode(params)


async def get_multiline(queries: List[str], geo: str, timeframe: str):
    import urllib.parse

    explore_url = build_explore_url(queries, geo, timeframe)
    status, text, retries = await fetch_with_backoff(explore_url)
    if status != 200:
        return {
            "ok": False,
            "stage": "explore",
            "http_status": status,
            "retries": retries,
        }

    try:
        explore = json.loads(strip_xssi(text))
    except Exception:
        return {
            "ok": False,
            "stage": "explore_parse",
            "http_status": status,
            "retries": retries,
        }

    widgets = explore.get("widgets", [])
    ts_widget = next(
        (w for w in widgets if w.get("id") == "TIMESERIES" or w.get("title") == "Interest over time"),
        None,
    )
    if not ts_widget:
        return {
            "ok": False,
            "stage": "timeseries_widget_missing",
            "http_status": 200,
            "retries": retries,
        }

    token = ts_widget.get("token")
    req = ts_widget.get("request")
    if not token or not req:
        return {
            "ok": False,
            "stage": "timeseries_widget_invalid",
            "http_status": 200,
            "retries": retries,
        }

    params = {
        "hl": "en-US",
        "tz": "240",
        "req": json.dumps(req, separators=(",", ":")),
        "token": token,
    }
    multiline_url = (
        "https://trends.google.com/trends/api/widgetdata/multiline?"
        + urllib.parse.urlencode(params)
    )

    status2, text2, retries2 = await fetch_with_backoff(multiline_url)
    if status2 != 200:
        return {
            "ok": False,
            "stage": "multiline",
            "http_status": status2,
            "retries": retries2,
        }

    try:
        payload = json.loads(strip_xssi(text2))
    except Exception:
        return {
            "ok": False,
            "stage": "multiline_parse",
            "http_status": status2,
            "retries": retries2,
        }

    timeline = payload.get("default", {}).get("timelineData", [])
    series = {q: [] for q in queries}
    for row in timeline:
        values = row.get("value", [])
        for i, q in enumerate(queries):
            if i < len(values):
                series[q].append(values[i])

    summary = {}
    for q, vals in series.items():
        if vals:
            n = len(vals)
            recent_window = vals[max(0, n - max(4, n // 8)):]
            early_window = vals[:max(4, n // 8)]
            avg = sum(vals) / len(vals)
            recent_avg = sum(recent_window) / len(recent_window)
            early_avg = sum(early_window) / len(early_window)
            delta = recent_avg - early_avg
            if delta > 5:
                direction = "UP"
            elif delta < -5:
                direction = "DOWN"
            else:
                direction = "FLAT"
            summary[q] = {
                "average": round(avg, 1),
                "recent_average": round(recent_avg, 1),
                "direction": direction,
                "points": n,
            }
        else:
            summary[q] = {
                "average": None,
                "recent_average": None,
                "direction": "UNAVAILABLE",
                "points": 0,
            }

    return {
        "ok": True,
        "http_status": 200,
        "retries": retries + retries2,
        "summary": summary,
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "KatMo Trends Collector",
        "version": "2.0.0",
        "rate_limit_strategy": "paced+exponential_backoff+cache",
    }


@app.post("/validate-candidate")
async def validate_candidate(
    payload: CandidateRequest,
    authorization: Optional[str] = Header(default=None),
):
    check_auth(authorization)

    # Clean query set and reduce duplicate request load.
    queries = []
    for q in payload.queries:
        q = q.strip()
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)
    queries = queries[:5]

    if not queries:
        raise HTTPException(status_code=400, detail="At least one query is required.")

    key = _cache_key(payload)
    cached = _cache.get(key)
    now = time.time()
    if cached and now - cached["stored_at"] < CACHE_TTL_SECONDS:
        result = dict(cached["result"])
        result["cache"] = "HIT"
        return result

    twelve = await get_multiline(queries, payload.geo, "today 12-m")

    # Avoid hitting Google immediately again after a 429-heavy result.
    if twelve.get("http_status") == 429:
        five = {
            "ok": False,
            "stage": "skipped_after_12m_rate_limit",
            "http_status": 429,
            "retries": 0,
        }
    elif payload.include_five_year:
        # Extra breathing room between large windows.
        await _page.wait_for_timeout(random.randint(5000, 8000))
        five = await get_multiline(queries, payload.geo, "today 5-y")
    else:
        five = {"ok": False, "stage": "not_requested", "http_status": None, "retries": 0}

    access = "FULL"
    if not twelve.get("ok") or (payload.include_five_year and not five.get("ok")):
        access = "PARTIAL"
    if not twelve.get("ok") and not five.get("ok"):
        access = "UNAVAILABLE"

    result = {
        "candidate_id": payload.candidate_id,
        "topic": payload.topic,
        "geo": payload.geo,
        "queries": queries,
        "twelve_month": twelve,
        "five_year": five,
        "trends_receipt": {
            "source": "Google Trends custom-query Explore backend",
            "access_status": access,
            "collector_version": "2.0.0",
            "retrieved_at_epoch": int(time.time()),
            "rate_limit_strategy": "paced+exponential_backoff+jitter+6h_cache",
        },
        "cache": "MISS",
    }

    _cache[key] = {"stored_at": now, "result": result}
    return result
