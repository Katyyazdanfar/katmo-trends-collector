import os
import json
import time
import random
import hashlib
import urllib.parse
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, BrowserContext

app = FastAPI(title="KatMo Trends Collector", version="3.0.0")

API_KEY = os.getenv("KATMO_API_KEY", "").strip()

REQUEST_GAP_SECONDS = float(os.getenv("TRENDS_REQUEST_GAP_SECONDS", "3.5"))
RETRY_BASE_SECONDS = float(os.getenv("TRENDS_RETRY_BASE_SECONDS", "8"))
MAX_RETRIES = int(os.getenv("TRENDS_MAX_RETRIES", "3"))
CACHE_TTL_SECONDS = int(os.getenv("TRENDS_CACHE_TTL_SECONDS", "21600"))

_cache: Dict[str, Dict[str, Any]] = {}
_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
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
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def cache_key(payload: CandidateRequest) -> str:
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
    global _pw, _browser, _context
    if _browser is not None and _context is not None:
        return

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True)
    _context = await _browser.new_context(
        user_agent=UA,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    # Warm session once to establish Google Trends cookies.
    page = await _context.new_page()
    try:
        await page.goto(
            "https://trends.google.com/trends/explore?geo=US",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await page.wait_for_timeout(2500)
    except Exception:
        pass
    finally:
        await page.close()


async def paced_wait():
    global _last_request_at
    await ensure_browser()
    now = time.monotonic()
    elapsed = now - _last_request_at

    if elapsed < REQUEST_GAP_SECONDS:
        await _context.request.get(
            "https://trends.google.com/robots.txt",
            timeout=15000
        ) if False else None
        await asyncio_sleep(REQUEST_GAP_SECONDS - elapsed)

    await asyncio_sleep(random.uniform(0.6, 1.4))
    _last_request_at = time.monotonic()


async def asyncio_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


async def request_text(url: str):
    """
    IMPORTANT:
    Uses BrowserContext.request instead of page.evaluate(fetch(...)).
    This avoids browser-side CORS/fetch failures on Render while retaining
    the browser context's cookies and headers.
    """
    await ensure_browser()
    await paced_wait()

    try:
        response = await _context.request.get(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://trends.google.com/trends/explore?geo=US",
            },
            timeout=45000,
            fail_on_status_code=False,
        )
        return response.status, await response.text()
    except Exception as e:
        return 599, f"{type(e).__name__}: {e}"


def strip_xssi(text: str) -> str:
    if text.startswith(")]}',"):
        return text.split("\n", 1)[1] if "\n" in text else text[5:]
    return text


async def fetch_with_backoff(url: str):
    last_status = None
    last_text = ""

    for attempt in range(MAX_RETRIES + 1):
        status, text = await request_text(url)
        last_status, last_text = status, text

        if status == 200:
            return status, text, attempt

        if status == 429 and attempt < MAX_RETRIES:
            delay = RETRY_BASE_SECONDS * (2 ** attempt) + random.uniform(1.0, 4.0)
            await asyncio_sleep(delay)
            continue

        break

    return last_status, last_text, MAX_RETRIES


def build_explore_url(queries: List[str], geo: str, timeframe: str) -> str:
    comparison = [{"keyword": q, "geo": geo, "time": timeframe} for q in queries]
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
    explore_url = build_explore_url(queries, geo, timeframe)
    status, text, retries = await fetch_with_backoff(explore_url)

    if status != 200:
        return {
            "ok": False,
            "stage": "explore",
            "http_status": status,
            "retries": retries,
            "error_excerpt": text[:180] if text else None,
        }

    try:
        explore = json.loads(strip_xssi(text))
    except Exception as e:
        return {
            "ok": False,
            "stage": "explore_parse",
            "http_status": status,
            "retries": retries,
            "error_excerpt": str(e)[:180],
        }

    widgets = explore.get("widgets", [])
    ts_widget = next(
        (
            w for w in widgets
            if w.get("id") == "TIMESERIES"
            or w.get("title") == "Interest over time"
        ),
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
            "error_excerpt": text2[:180] if text2 else None,
        }

    try:
        payload = json.loads(strip_xssi(text2))
    except Exception as e:
        return {
            "ok": False,
            "stage": "multiline_parse",
            "http_status": status2,
            "retries": retries2,
            "error_excerpt": str(e)[:180],
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
            window = max(4, n // 8)
            recent = vals[-window:]
            early = vals[:window]
            avg = sum(vals) / n
            recent_avg = sum(recent) / len(recent)
            early_avg = sum(early) / len(early)
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


@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "KatMo Trends Collector",
        "version": "3.0.0",
        "health": "/health",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "KatMo Trends Collector",
        "version": "3.0.0",
        "fetch_strategy": "playwright-context-request",
        "rate_limit_strategy": "paced+exponential_backoff+cache",
    }


@app.post("/validate-candidate")
async def validate_candidate(
    payload: CandidateRequest,
    authorization: Optional[str] = Header(default=None),
):
    check_auth(authorization)

    queries = []
    seen = set()
    for raw in payload.queries:
        q = raw.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    queries = queries[:5]

    if not queries:
        raise HTTPException(status_code=400, detail="At least one query is required.")

    key = cache_key(payload)
    cached = _cache.get(key)
    now = time.time()

    if cached and now - cached["stored_at"] < CACHE_TTL_SECONDS:
        result = dict(cached["result"])
        result["cache"] = "HIT"
        return result

    twelve = await get_multiline(queries, payload.geo, "today 12-m")

    if payload.include_five_year:
        # Avoid hammering Google immediately if the first window is already rate-limited.
        if twelve.get("http_status") == 429:
            five = {
                "ok": False,
                "stage": "skipped_after_12m_rate_limit",
                "http_status": 429,
                "retries": 0,
            }
        else:
            await asyncio_sleep(random.uniform(5.0, 8.0))
            five = await get_multiline(queries, payload.geo, "today 5-y")
    else:
        five = {
            "ok": False,
            "stage": "not_requested",
            "http_status": None,
            "retries": 0,
        }

    if twelve.get("ok") and (five.get("ok") or not payload.include_five_year):
        access = "FULL"
    elif twelve.get("ok") or five.get("ok"):
        access = "PARTIAL"
    else:
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
            "collector_version": "3.0.0",
            "retrieved_at_epoch": int(time.time()),
            "fetch_strategy": "playwright-context-request",
            "rate_limit_strategy": "paced+exponential_backoff+jitter+6h_cache",
        },
        "cache": "MISS",
    }

    _cache[key] = {"stored_at": now, "result": result}
    return result
