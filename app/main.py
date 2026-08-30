import json
import os
import urllib.parse
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

APP_VERSION = "1.0.0"
API_KEY = os.getenv("KATMO_API_KEY", "")

app = FastAPI(
    title="KatMo Google Trends Collector",
    version=APP_VERSION,
    description="Collects real Google Trends custom-query data for KatMo candidate validation."
)

class CandidateRequest(BaseModel):
    candidate_id: str
    topic: str
    queries: List[str] = Field(..., min_length=3, max_length=5)
    geo: str = "US"
    include_five_year: bool = True

def check_auth(authorization: Optional[str]):
    if not API_KEY:
        return
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")

def strip_xssi(text: str):
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("No JSON payload in Google Trends response")
    return text[min(starts):]

def summarize(timeline, queries):
    values = {q: [] for q in queries}

    for row in timeline or []:
        row_values = row.get("value", [])
        for i, q in enumerate(queries):
            if i < len(row_values):
                try:
                    values[q].append(float(row_values[i]))
                except Exception:
                    pass

    stats = {}
    for q, arr in values.items():
        if not arr:
            stats[q] = {
                "mean": 0,
                "recent_mean": 0,
                "early_mean": 0,
                "delta_recent_vs_early": 0,
                "peak": 0,
                "nonzero_share": 0,
            }
            continue

        n = len(arr)
        k = max(1, min(12, n // 4 if n >= 4 else 1))
        early = sum(arr[:k]) / k
        recent = sum(arr[-k:]) / k
        mean = sum(arr) / n
        peak = max(arr)
        nonzero_share = sum(1 for x in arr if x > 0) / n

        stats[q] = {
            "mean": round(mean, 2),
            "recent_mean": round(recent, 2),
            "early_mean": round(early, 2),
            "delta_recent_vs_early": round(recent - early, 2),
            "peak": round(peak, 2),
            "nonzero_share": round(nonzero_share, 3),
        }

    strongest = max(stats.items(), key=lambda x: x[1]["mean"])[0] if stats else None
    s = stats.get(strongest, {})

    mean = s.get("mean", 0)
    delta = s.get("delta_recent_vs_early", 0)
    peak = s.get("peak", 0)
    recent = s.get("recent_mean", 0)
    nz = s.get("nonzero_share", 0)

    if nz < 0.25 or mean < 2:
        signal = "WEAK"
    elif delta >= max(5, mean * 0.35):
        signal = "STRONG_RISING"
    elif peak >= max(50, mean * 4) and recent < peak * 0.35:
        signal = "EVENT_SPIKE"
    elif nz >= 0.75 and abs(delta) <= max(5, mean * 0.25):
        signal = "HEALTHY_STABLE"
    else:
        signal = "INCONCLUSIVE"

    return {
        "signal": signal,
        "strongest_query": strongest,
        "series_summary": stats,
    }

async def get_json(request_context, url):
    response = await request_context.get(url, timeout=30000)

    if response.status == 429:
        return None, 429

    if not response.ok:
        return None, response.status

    text = await response.text()
    return json.loads(strip_xssi(text)), response.status

async def collect_trends(candidate: CandidateRequest):
    queries = [q.strip() for q in candidate.queries if q.strip()][:5]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        warm_url = "https://trends.google.com/trends/explore?geo=US&hl=en-US"

        try:
            try:
                await page.goto(warm_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)
            except Exception:
                pass

            result = {
                "candidate_id": candidate.candidate_id,
                "topic": candidate.topic,
                "queries_tested": queries,
                "geo": candidate.geo,
                "route": "RENDER_PLAYWRIGHT_DIRECT_JSON",
                "access_status": "OK",
                "windows": {},
            }

            windows = [("12m", "today 12-m")]
            if candidate.include_five_year:
                windows.append(("5y", "today 5-y"))

            for label, timeframe in windows:
                req_obj = {
                    "comparisonItem": [
                        {"keyword": q, "geo": candidate.geo, "time": timeframe}
                        for q in queries
                    ],
                    "category": 0,
                    "property": "",
                }

                encoded = urllib.parse.quote(json.dumps(req_obj, separators=(",", ":")))
                explore_url = (
                    "https://trends.google.com/trends/api/explore"
                    f"?hl=en-US&tz=360&req={encoded}"
                )

                explore_data, status = await get_json(context.request, explore_url)

                if explore_data is None and status == 429:
                    try:
                        await page.goto(warm_url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    explore_data, status = await get_json(context.request, explore_url)

                if explore_data is None:
                    result["access_status"] = "PARTIAL" if result["windows"] else "UNAVAILABLE"
                    result["windows"][label] = {
                        "status": "UNAVAILABLE",
                        "http_status": status,
                    }
                    continue

                widgets = explore_data.get("widgets", [])
                timeseries = next((w for w in widgets if w.get("id") == "TIMESERIES"), None)

                if not timeseries:
                    result["windows"][label] = {
                        "status": "INCONCLUSIVE",
                        "note": "TIMESERIES widget missing",
                    }
                    continue

                widget_req = urllib.parse.quote(
                    json.dumps(timeseries["request"], separators=(",", ":"))
                )
                token = urllib.parse.quote(timeseries["token"], safe="")

                multiline_url = (
                    "https://trends.google.com/trends/api/widgetdata/multiline"
                    f"?hl=en-US&tz=360&req={widget_req}&token={token}"
                )

                multiline_data, multiline_status = await get_json(
                    context.request, multiline_url
                )

                if multiline_data is None:
                    result["windows"][label] = {
                        "status": "UNAVAILABLE",
                        "http_status": multiline_status,
                    }
                    result["access_status"] = "PARTIAL"
                    continue

                timeline = multiline_data.get("default", {}).get("timelineData", [])
                summary = summarize(timeline, queries)

                result["windows"][label] = {
                    "status": "OK",
                    "signal": summary["signal"],
                    "strongest_query": summary["strongest_query"],
                    "series_summary": summary["series_summary"],
                    "points": len(timeline),
                }

            w12 = result["windows"].get("12m", {})
            w5 = result["windows"].get("5y", {})

            result["trends_receipt"] = {
                "candidate_id": candidate.candidate_id,
                "route": result["route"],
                "queries_tested": queries,
                "geography": candidate.geo,
                "access_status": result["access_status"],
                "signal_12m": w12.get("signal", w12.get("status", "UNAVAILABLE")),
                "signal_5y": w5.get("signal", w5.get("status", "UNAVAILABLE")),
                "strongest_wording_12m": w12.get("strongest_query"),
                "strongest_wording_5y": w5.get("strongest_query"),
                "source": "Google Trends",
            }

            return result

        finally:
            await browser.close()

@app.get("/health", operation_id="healthCheck")
async def health():
    return {
        "ok": True,
        "service": "KatMo Trends Collector",
        "version": APP_VERSION,
    }

@app.post("/validate-candidate", operation_id="validateCandidateTrends")
async def validate_candidate(
    candidate: CandidateRequest,
    authorization: Optional[str] = Header(default=None),
):
    check_auth(authorization)

    try:
        return await collect_trends(candidate)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Google Trends collection failed: {type(e).__name__}: {e}",
        )
