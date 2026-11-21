import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from curl_cffi import requests as browser_requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="GoldLens — jeweller gold rates", version="3.2.0")

# Only jewellers with a browser-verified gold-rate section and a stable,
# automatically readable rate source are included.
SOURCES: dict[str, dict[str, Any]] = {
    "Tanishq": {
        "adapter": "tanishq",
        "url": "https://www.tanishq.co.in/gold-rate.html?lang=en_IN",
        "scope": "India",
    },
    "Dhan": {
        "adapter": "dhan",
        "url": "https://dhan.co/gold-rates/",
        "scope": "India",
    },
    "Malabar Gold & Diamonds": {
        "adapter": "malabar",
        "url": "https://www.malabargoldanddiamonds.com/in/pan-india/en/live-gold-rate.html",
        "api_url": "https://www.malabargoldanddiamonds.com/graphql-magento",
        "scope": "Pan India",
    },
    "CaratLane": {
        "adapter": "html",
        "url": "https://www.caratlane.com/gold-rate",
        "pattern": r'"goldRateDetails"\s*:\s*\{[^}]*"rate_24kt"\s*:\s*([\d,.]+)',
        "scope": "India default; may vary by city",
    },
    "Candere by Kalyan": {
        "adapter": "html",
        "url": "https://www.candere.com/gold-rate-today/mumbai",
        "pattern": r'id="goldPrice24k"\s+data-rate="([\d,.]+)"',
        "scope": "Mumbai",
    },
    "Indriya": {
        "adapter": "indriya",
        "url": "https://www.indriya.com/gold-rate-today",
        "api_url": "https://www.indriya.com/content/noveljewels/in/en/gold-rate-today/jcr:content/root/container/container/container_copy_copy__1497933197/chartcomponent.goldChart.json",
        "scope": "Published store rate",
    },
    "PNG Jewellers": {
        "adapter": "html",
        "url": "https://www.pngjewellers.com/",
        "pattern": r'24K Gold\s*<span[^>]*class="price"[^>]*>\s*₹\s*([\d,.]+)',
        "scope": "Online purchase",
    },
}


class GoldRate(BaseModel):
    jeweller: str
    rate: float | None = None
    purity: str = "24K / 999"
    unit: str = "per gram"
    scope: str
    status: str
    source_url: str
    fetched_at: str
    published_at: str | None = None
    note: str = "Published gold rate only; jewellery price, making charges, GST, and other fees are excluded."


cache: dict[str, Any] = {}
cache_at = 0.0
cache_lock = asyncio.Lock()


def parse_rate(value: Any) -> float | None:
    match = re.search(r"[\d,]+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    rate = float(match.group(0).replace(",", ""))
    return rate if 1_000 <= rate <= 100_000 else None


def extract_tanishq_rate(html: str) -> float | None:
    """The first rate row is today's 1 g value; all rows carry each purity."""
    match = re.search(r'data-goldrate24kt="([\d,.]+)"', html, re.I)
    return parse_rate(match.group(1) if match else None)


def extract_dhan_rate(html: str) -> tuple[float | None, str | None]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.I | re.S)
    if not match:
        return None, None
    payload = json.loads(match.group(1))
    page = payload.get("props", {}).get("pageProps", {})
    timestamp = page.get("snapshotTimestamp")
    rows = page.get("apiData", {}).get("data", [])
    row = next((item for item in rows if item.get("city") == "delhi" and item.get("timestamp") == timestamp), None)
    rate = parse_rate((row or {}).get("close"))
    published = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp else None
    return rate, published


async def fetch_rate(client: httpx.AsyncClient, jeweller: str, cfg: dict[str, Any]) -> GoldRate:
    base = {
        "jeweller": jeweller,
        "scope": cfg["scope"],
        "source_url": cfg["url"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if cfg["adapter"] == "tanishq":
            response = await asyncio.to_thread(browser_requests.get, cfg["url"], impersonate="chrome", timeout=20)
            if response.status_code >= 400:
                raise ValueError(f"Tanishq returned HTTP {response.status_code}")
            rate = extract_tanishq_rate(response.text)
            if not rate:
                raise ValueError("24K rate missing")
            return GoldRate(**base, rate=rate, status="available")

        if cfg["adapter"] == "dhan":
            response = await client.get(cfg["url"], timeout=15, follow_redirects=True)
            response.raise_for_status()
            rate, published = extract_dhan_rate(response.text)
            if not rate:
                raise ValueError("24K rate missing")
            return GoldRate(**base, rate=rate, status="available", published_at=published)

        if cfg["adapter"] == "malabar":
            query = "query getMetalRate($filter: MetalRateFilterInput) { getMetalRate(filter: $filter) { items { entry_date entry_time purity unit rate country state } } }"
            variables = {"filter": {"metal_type": "gold", "country": "India"}}
            response = await client.get(cfg["api_url"], params={"query": query, "variables": json.dumps(variables, separators=(",", ":"))}, headers={"Referer": cfg["url"]}, timeout=15)
            response.raise_for_status()
            items = response.json().get("data", {}).get("getMetalRate", {}).get("items", [])
            item = next((row for row in items if str(row.get("purity", "")).lower() == "24k" and str(row.get("unit", "")).upper() == "G"), None)
            rate = parse_rate((item or {}).get("rate"))
            if not rate:
                raise ValueError("24K rate missing")
            published = f"{item['entry_date']}T{item['entry_time']}+05:30" if item.get("entry_date") and item.get("entry_time") else None
            return GoldRate(**base, rate=rate, status="available", published_at=published)

        if cfg["adapter"] == "indriya":
            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            params = {"storeId": "NS0001", "fromDate": (today - timedelta(days=1)).isoformat(), "toDate": today.isoformat()}
            response = await client.get(cfg["api_url"], params=params, headers={"Referer": cfg["url"]}, timeout=15)
            response.raise_for_status()
            payload = json.loads(response.json().get("ratesJson", "{}"))
            purity = next((row for row in payload.get("purities", []) if row.get("purity") == "24KT 999"), None)
            details = (purity or {}).get("data", {}).get("getGoldRate", {}).get("gold_rate", {})
            rate = parse_rate(details.get("today"))
            if not rate:
                raise ValueError("24K rate missing")
            published = details.get("last_refreshed") or details.get("lastRefreshed")
            return GoldRate(**base, rate=rate, status="available", published_at=published)

        response = await client.get(cfg["url"], timeout=15, follow_redirects=True)
        response.raise_for_status()
        match = re.search(cfg["pattern"], response.text, re.I | re.S)
        rate = parse_rate(match.group(1) if match else None)
        if not rate:
            raise ValueError("24K rate missing")
        return GoldRate(**base, rate=rate, status="available")
    except httpx.HTTPStatusError as exc:
        return GoldRate(**base, status="unavailable", note=f"Rate page returned HTTP {exc.response.status_code}.")
    except httpx.TimeoutException:
        return GoldRate(**base, status="unavailable", note="Rate page timed out.")
    except httpx.RequestError:
        return GoldRate(**base, status="unavailable", note="Rate page could not be reached.")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return GoldRate(**base, status="unavailable", note="The published 24K rate was missing or changed format.")


@app.get("/api/rates")
@app.get("/api/offers")
async def rates(refresh: bool = False):
    global cache, cache_at
    if not refresh and cache and time.time() - cache_at < 300:
        return cache
    async with cache_lock:
        if not refresh and cache and time.time() - cache_at < 300:
            return cache
        headers = {
            "User-Agent": os.getenv("GOLDLENS_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.8",
        }
        async with httpx.AsyncClient(headers=headers) as client:
            checked = await asyncio.gather(*(fetch_rate(client, name, cfg) for name, cfg in SOURCES.items()))
        available = sorted((rate for rate in checked if rate.rate is not None), key=lambda row: (row.rate, row.jeweller))
        cache = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "rates": [rate.model_dump() for rate in available],
            "offers": [rate.model_dump() for rate in available],
            "summary": {"sources": len(SOURCES), "rates_found": len(available), "prices_found": len(available), "lowest_rate": available[0].rate if available else None, "automatic_adapters": len(SOURCES)},
            "note": "Only jeweller-published 24K gold rates are shown. Product premiums, making charges, GST, and checkout totals are excluded.",
        }
        cache_at = time.time()
        return cache


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version, "sources": len(SOURCES)}


HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GoldLens · Jeweller Gold Rates</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e9edf5;font:15px system-ui,Arial}.wrap{max-width:1080px;margin:auto;padding:32px 20px}header{display:flex;justify-content:space-between;gap:20px;align-items:center}h1{font-size:32px;margin:34px 0 8px}.muted{color:#aab5c8}.pill{border:1px solid #4b5563;padding:7px 12px;border-radius:40px}.panel{background:#151f30;border:1px solid #2b384d;border-radius:16px;padding:20px;margin:20px 0}.controls{display:flex;flex-wrap:wrap;align-items:center;gap:14px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.metric{background:#1b293e;padding:18px;border-radius:12px}.metric b{display:block;font-size:23px;margin-top:8px;color:#f3ca78}button{background:#edc46c;color:#16120a;border:0;border-radius:9px;padding:11px 18px;font-weight:700;cursor:pointer}button:disabled{opacity:.6}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:15px 11px;border-bottom:1px solid #2b384d;vertical-align:top}th{color:#aab5c8}a{color:#f4cf7b}.available{color:#8ee6b0}.scroll{overflow:auto}.note{font-size:13px;max-width:360px;margin-top:5px}@media(max-width:720px){.metrics{grid-template-columns:1fr}h1{font-size:26px}}
</style><div class="wrap"><header><div><strong style="color:#f4cf7b">◈ GOLDLENS</strong><div class="muted">India · jeweller-published gold rates</div></div><span class="pill">24K · per gram</span></header><h1>Compare published 24K gold rates.</h1><p class="muted">Rates come from each jeweller's own gold-rate section—not from coin or jewellery product listings.</p><div class="panel controls"><button id="refresh">Refresh rates</button><span id="updated" class="muted"></span></div><div class="metrics"><div class="metric">Lowest published rate<b id="lowest">—</b></div><div class="metric">Rates available<b id="found">—</b></div><div class="metric">Verified rate sources<b id="sources">—</b></div></div><div class="panel"><h2>Jeweller gold rates</h2><div class="scroll"><table><thead><tr><th>Jeweller</th><th>24K rate</th><th>Scope</th><th>Details</th><th>Source</th></tr></thead><tbody id="rows"><tr><td colspan="5">Loading…</td></tr></tbody></table></div></div><p class="muted">These are reference gold rates, not final jewellery or coin prices. Making charges, premiums, GST, stones, delivery, and other fees are not included.</p></div><script>
const money=n=>n==null?'—':new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:2}).format(n);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(refresh=false){const status=document.getElementById('updated'),button=document.getElementById('refresh');status.textContent='Checking…';button.disabled=true;try{const response=await fetch('/api/rates'+(refresh?'?refresh=true':''));if(!response.ok)throw new Error();const data=await response.json();document.getElementById('lowest').textContent=money(data.summary.lowest_rate)+'/g';document.getElementById('found').textContent=data.summary.rates_found+' / '+data.summary.sources;document.getElementById('sources').textContent=data.summary.sources;status.textContent='Checked '+new Date(data.updated_at).toLocaleString();document.getElementById('rows').innerHTML=data.rates.map(x=>`<tr><td><strong>${esc(x.jeweller)}</strong></td><td><strong>${money(x.rate)}</strong><div class="muted">per gram</div></td><td>${esc(x.scope)}</td><td class="available">published<div class="muted note">${esc(x.purity)} · ${esc(x.note)}</div></td><td><a href="${esc(x.source_url)}" target="_blank" rel="noopener noreferrer">View rate ↗</a></td></tr>`).join('')||'<tr><td colspan="5">No published rates are currently available.</td></tr>'}catch(e){status.textContent='Could not load rates'}finally{button.disabled=false}}
document.getElementById('refresh').addEventListener('click',()=>load(true));load();
</script></html>'''


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML
