import asyncio
import json

import httpx

from app import SOURCES, extract_dhan_rate, extract_tanishq_rate, fetch_rate, parse_rate


def run_fetch(name, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_rate(client, name, SOURCES[name])
    return asyncio.run(run())


def test_parse_rate():
    assert parse_rate("₹12,516.50") == 12516.5
    assert parse_rate("missing") is None


def test_tanishq_embedded_24k_rate():
    html = '<td data-goldrate18kt="9421" data-goldrate22kt="11515" data-goldrate24kt="12562">11515</td>'
    assert extract_tanishq_rate(html) == 12562


def test_dhan_next_data_rate():
    payload = {"props": {"pageProps": {"snapshotTimestamp": 1795113000000, "apiData": {"data": [
        {"city": "mumbai", "timestamp": 1795113000000, "close": 12275.7},
        {"city": "delhi", "timestamp": 1795113000000, "close": 12295.3},
    ]}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    rate, published = extract_dhan_rate(html)
    assert rate == 12295.3
    assert published == "2026-11-19T18:30:00+00:00"


def test_caratlane_html_rate():
    html = '<script>"goldRateDetails":{"rate_24kt":12707,"rate_22kt":11648}</script>'
    result = run_fetch("CaratLane", lambda request: httpx.Response(200, request=request, text=html))
    assert result.rate == 12707


def test_candere_html_rate():
    html = '<p id="goldPrice24k" data-rate="12516">₹12,516</p>'
    result = run_fetch("Candere by Kalyan", lambda request: httpx.Response(200, request=request, text=html))
    assert result.rate == 12516
    assert result.scope == "Mumbai"


def test_png_html_rate():
    html = '<li>24K Gold <span class="price" style="color:black">₹12357</span></li>'
    result = run_fetch("PNG Jewellers", lambda request: httpx.Response(200, request=request, text=html))
    assert result.rate == 12357


def test_indriya_rate_api():
    rates = {"purities": [{"purity": "24KT 999", "data": {"getGoldRate": {"gold_rate": {"today": "12503.86", "last_refreshed": "21 Nov 2026 12:00 PM"}}}}]}
    result = run_fetch("Indriya", lambda request: httpx.Response(200, request=request, json={"ratesJson": json.dumps(rates)}))
    assert result.rate == 12503.86
    assert result.published_at == "21 Nov 2026 12:00 PM"


def test_malabar_rate_api():
    payload = {"data": {"getMetalRate": {"items": [
        {"entry_date": "2026-11-21", "entry_time": "09:44:10", "purity": "22k", "unit": "G", "rate": "11465.00"},
        {"entry_date": "2026-11-21", "entry_time": "09:44:10", "purity": "24k", "unit": "G", "rate": "12507.00"},
    ]}}}
    result = run_fetch("Malabar Gold & Diamonds", lambda request: httpx.Response(200, request=request, json=payload))
    assert result.rate == 12507
    assert result.published_at == "2026-11-21T09:44:10+05:30"


def test_failed_rate_is_unavailable():
    result = run_fetch("PNG Jewellers", lambda request: httpx.Response(503, request=request))
    assert result.rate is None
    assert result.status == "unavailable"
    assert "503" in result.note
