#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — ตัวเก็บข้อมูลดิบตลาดคริปโต (T019 ราง A)

กฎเหล็ก: เก็บ response ดิบทั้งก้อน ห้ามเก็บแค่เลขที่คำนวณแล้ว
         (สูตรผิดเมื่อไหร่ ย้อนคำนวณใหม่จากดิบได้เสมอ · IV ย้อนหลังไม่ได้ ไม่เก็บวันนี้ = หายถาวร)

ใช้ stdlib ล้วน ไม่ต้อง pip install อะไรเลย
ผลลัพธ์: out/raw/<YYYY>/<MM>/<DD>/<HHMM>Z/<name>.json.xz  +  _meta.json
"""

import json
import lzma
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT = 30          # วินาที ต่อ 1 request
RETRIES = 3           # ยิงซ้ำสูงสุดกี่ครั้งถ้าล้ม
BACKOFF = 4           # วินาที รอก่อนยิงซ้ำ (คูณตามรอบ)
UA = "crypto-raw-collector/1.0 (personal market data archive)"

# (ชื่อไฟล์, [url หลัก, url สำรอง...], ชนิดการตรวจ)  — ยิงทดสอบจริงครบทุกตัวแล้ว 10 ก.ย. 2026 ไม่ต้องใช้ API key
# ⚠️ api.binance.com ตอบ HTTP 451 เมื่อยิงจาก IP อเมริกา (runner ของ GitHub อยู่ US)
#    จึงใส่ data-api.binance.vision (ตัวสะท้อนข้อมูลตลาดของ Binance เอง) เป็นทางสำรอง
ENDPOINTS = [
    # ราคา (อ้างอิงเวลาของ snapshot — klines ย้อนหลังได้ ไม่ต้องเก็บ)
    ("binance_ticker24h_btc", ["https://data-api.binance.vision/api/v3/ticker/24hr?symbol=BTCUSDT",
                               "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"], "binance"),
    ("binance_ticker24h_eth", ["https://data-api.binance.vision/api/v3/ticker/24hr?symbol=ETHUSDT",
                               "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT"], "binance"),
    # ราคาจาก OKX — ตัวยืนพื้น ไม่เคยโดนบล็อกตามภูมิภาค
    ("okx_ticker_swap_btc", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP", "okx"),
    ("okx_ticker_swap_eth", "https://www.okx.com/api/v5/market/ticker?instId=ETH-USDT-SWAP", "okx"),
    ("okx_index_btc", "https://www.okx.com/api/v5/market/index-tickers?instId=BTC-USD", "okx"),
    ("okx_index_eth", "https://www.okx.com/api/v5/market/index-tickers?instId=ETH-USD", "okx"),
    # funding (รอบปัจจุบัน + ย้อนหลัง 100 รอบ ≈ 33 วัน — ใช้คิดเปอร์เซ็นไทล์ให้ตรงกับวิธี backtest)
    ("okx_funding_btc", "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", "okx"),
    ("okx_funding_eth", "https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP", "okx"),
    ("okx_funding_hist_btc", "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100", "okx"),
    ("okx_funding_hist_eth", "https://www.okx.com/api/v5/public/funding-rate-history?instId=ETH-USDT-SWAP&limit=100", "okx"),
    # open interest ฝั่ง perp
    ("okx_oi_swap_btc", "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP", "okx"),
    ("okx_oi_swap_eth", "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=ETH-USDT-SWAP", "okx"),
    # options — ตัวหลักของงานนี้ (IV + greeks + OI รายstrike)
    ("okx_optsum_btc", "https://www.okx.com/api/v5/public/opt-summary?uly=BTC-USD", "okx"),
    ("okx_optsum_eth", "https://www.okx.com/api/v5/public/opt-summary?uly=ETH-USD", "okx"),
    ("okx_opt_oi_btc", "https://www.okx.com/api/v5/public/open-interest?instType=OPTION&uly=BTC-USD", "okx"),
    ("okx_opt_oi_eth", "https://www.okx.com/api/v5/public/open-interest?instType=OPTION&uly=ETH-USD", "okx"),
    # options แหล่งที่ 2 — ไว้ cross-check และสำรองเวลา OKX ล่ม
    ("deribit_opt_btc", "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option", "deribit"),
    ("deribit_opt_eth", "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=ETH&kind=option", "deribit"),
]


def fetch_one(url):
    """ยิง url เดียวพร้อม retry — คืน (bytes, http_status) หรือโยน exception ถ้าหมดโควตา"""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            last = e
            # 4xx ที่ไม่ใช่ rate-limit ยิงซ้ำก็ไม่ช่วย (451 = โดนบล็อกตามภูมิภาค)
            if e.code not in (408, 429) and 400 <= e.code < 500:
                raise
        except Exception as e:  # timeout / DNS / connection reset
            last = e
        if attempt < RETRIES:
            time.sleep(BACKOFF * attempt)
    raise last


def fetch(urls):
    """ไล่ยิงตามลำดับ url จนกว่าจะได้ — คืน (bytes, http_status, url ที่ใช้จริง)"""
    if isinstance(urls, str):
        urls = [urls]
    last = None
    for i, url in enumerate(urls):
        try:
            body, status = fetch_one(url)
            return body, status, url
        except Exception as e:
            last = e
            if i + 1 < len(urls):
                print("    ทางหลักล้ม (%s) -> ลองทางสำรอง" % e, flush=True)
    raise last


def check_payload(body, kind):
    """ตรวจว่าเนื้อที่ได้ 'ใช้ได้จริง' ไม่ใช่หน้า error ที่ตอบ 200 — คืน (ok, เหตุผล, จำนวนแถว)"""
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception as e:
        return False, "parse ไม่ได้: %s" % e, None

    if kind == "okx":
        if str(obj.get("code")) != "0":
            return False, "okx code=%s msg=%s" % (obj.get("code"), obj.get("msg")), None
        data = obj.get("data")
        if not isinstance(data, list) or len(data) == 0:
            return False, "okx data ว่าง", 0
        return True, "", len(data)

    if kind == "deribit":
        res = obj.get("result")
        if not isinstance(res, list) or len(res) == 0:
            return False, "deribit result ว่าง/ไม่ใช่ list", 0
        return True, "", len(res)

    if kind == "binance":
        if isinstance(obj, dict) and "code" in obj and "msg" in obj:
            return False, "binance error %s" % obj.get("msg"), None
        return True, "", 1 if isinstance(obj, dict) else len(obj)

    return True, "", None


def main():
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y/%m/%d/%H%M") + "Z"
    outdir = Path(os.environ.get("OUT_DIR", "out")) / "raw" / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    results = []
    n_ok = 0
    for name, url, kind in ENDPOINTS:
        t0 = time.time()
        rec = {"name": name, "url": url if isinstance(url, str) else url[0], "source": kind}
        try:
            body, status, used = fetch(url)
            rec["url_used"] = used
            ok, why, rows = check_payload(body, kind)
            # เซฟทั้งกรณีดีและกรณีเสีย — ของเสียก็เป็นหลักฐานว่าวันนั้นเกิดอะไรขึ้น
            fname = "%s.json.xz" % name if ok else "%s.BAD.json.xz" % name
            blob = lzma.compress(body, preset=9)
            (outdir / fname).write_bytes(blob)
            rec.update({
                "ok": ok, "http_status": status, "rows": rows,
                "bytes_raw": len(body), "bytes_xz": len(blob),
                "sha256_raw": hashlib.sha256(body).hexdigest(),
                "file": fname,
            })
            if not ok:
                rec["error"] = why
            else:
                n_ok += 1
        except Exception as e:
            rec.update({"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                        "http_status": getattr(e, "code", None)})
        rec["ms"] = int((time.time() - t0) * 1000)
        results.append(rec)
        print("%-24s %s  %s" % (
            name, "OK " if rec.get("ok") else "FAIL",
            ("%7d B -> %6d B  %4d ms" % (rec.get("bytes_raw", 0), rec.get("bytes_xz", 0), rec["ms"]))
            if rec.get("ok") else rec.get("error", "")), flush=True)

    meta = {
        "collected_at_utc": started.isoformat(),
        "stamp": stamp,
        "n_endpoints": len(ENDPOINTS),
        "n_ok": n_ok,
        "n_fail": len(ENDPOINTS) - n_ok,
        "total_bytes_raw": sum(r.get("bytes_raw", 0) for r in results),
        "total_bytes_xz": sum(r.get("bytes_xz", 0) for r in results),
        "collector_version": "1.0",
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "endpoints": results,
    }
    (outdir / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== %s : ok %d/%d · raw %.2f MB -> xz %.0f KB ===" % (
        stamp, n_ok, len(ENDPOINTS),
        meta["total_bytes_raw"] / 1048576, meta["total_bytes_xz"] / 1024))

    # ล้มทั้งกระดาน = ต้องดังให้ GitHub ส่งเมลเตือน · ล้มบางตัว = บันทึกไว้แล้วไปต่อ
    if n_ok == 0:
        print("ERROR: ดึงไม่สำเร็จสักตัวเดียว", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
