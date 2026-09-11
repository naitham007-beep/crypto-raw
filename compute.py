#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compute.py — แปลง snapshot ดิบให้เป็น "สิ่งที่ต้องทำอะไรกับมัน" (T019 ราง B)

อ่านไฟล์ที่ collect.py เพิ่งเก็บ → คำนวณ → เขียน
  $OUT_DIR/web/latest.json    สภาพตลาดล่าสุด + เช็กลิสต์ + ระดับหลักฐานของแต่ละข้อ
  $OUT_DIR/web/history.json   ค่าที่คำนวณแล้วย้อนหลัง (ไว้ทำเส้นเวลา "อะไรเปลี่ยนไป")

หลักที่ยึด:
  - ทุกข้อบนหน้าเว็บต้องบอกได้ว่า "รู้ได้ยังไง" → ทุก check มี evidence level ติดมาด้วย
  - ไม่มีสถิติรองรับ = พูดได้แค่ข้อเท็จจริง ห้ามแปลงเป็นคำแนะนำ
"""

import json
import lzma
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
REPO = os.environ.get("GITHUB_REPOSITORY", "korntrade/crypto-raw")
HISTORY_KEEP_DAYS = 120
# โซนตรึง: ขยายจาก strike ที่ gamma×OI สูงสุดไปทีละ strike ที่ติดกัน ตราบที่ยัง ≥ สัดส่วนนี้ของจุดสูงสุด
# ต้องตรงกับ PIN_KEEP ใน fallback_cloudflare/worker.js (Worker คำนวณชุดเดียวกันทุก 15 นาที)
PIN_KEEP = 0.5
UA = "crypto-raw-compute/1.0"

# ผล backtest ที่รันไว้ (analysis/backtest_funding.ps1) — ใช้ตัดสินว่าข้อไหน "พิสูจน์แล้ว"
# ค่าพวกนี้มาจากข้อมูลจริง 3 เดือน และตรวจซ้ำด้วยการแบ่งครึ่งตัวอย่างแล้ว
FUNDING_EVIDENCE = {
    "BTC": {
        "baseline_up8h": 56.0, "n": 192, "window": "3 เดือน (มิ.ย.–ก.ย. 2026)",
        "buckets": {
            "hot":  {"up8h": 71.7, "n": 46, "halves": [70.0, 73.1], "stable": True},
            "warm": {"up8h": 37.8, "n": 37, "halves": [47.1, 30.0], "stable": True},
            "mid":  {"up8h": 65.7, "n": 35, "halves": [66.7, 64.7], "stable": True},
            "cool": {"up8h": 51.3, "n": 40, "halves": [43.5, 62.5], "stable": False},
            "cold": {"up8h": 50.0, "n": 34, "halves": [55.6, 43.8], "stable": False},
        },
    },
    "ETH": {
        "baseline_up8h": 53.9, "n": 192, "window": "3 เดือน (มิ.ย.–ก.ย. 2026)",
        "buckets": {
            "hot":  {"up8h": 57.1, "n": 49, "halves": [63.6, 55.3], "stable": False},
            "warm": {"up8h": 68.3, "n": 41, "halves": [76.2, 60.0], "stable": False},
            "mid":  {"up8h": 39.5, "n": 38, "halves": [36.4, 43.8], "stable": False},
            "cool": {"up8h": 37.8, "n": 37, "halves": [42.9, 31.2], "stable": False},
            "cold": {"up8h": 69.2, "n": 27, "halves": [66.7, 80.0], "stable": False},
        },
    },
}


# ---------- อ่านไฟล์ดิบ ----------

def newest_snapshot_dir():
    dirs = [p for p in (OUT_DIR / "raw").rglob("*") if p.is_dir() and (p / "_meta.json").exists()]
    if not dirs:
        raise SystemExit("ไม่พบ snapshot ใน $OUT_DIR/raw (รัน collect.py ก่อน)")
    return sorted(dirs)[-1]


def load(snapdir, name):
    p = snapdir / ("%s.json.xz" % name)
    if not p.exists():
        return None
    try:
        return json.loads(lzma.decompress(p.read_bytes()).decode("utf-8"))
    except Exception as e:
        print("อ่าน %s ไม่ได้: %s" % (name, e), file=sys.stderr)
        return None


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- ตัวช่วย ----------

def parse_inst(inst_id):
    """BTC-USD_UM-260911-78000-C -> (expiry '260911', strike 78000.0, 'C')  · รองรับทั้ง coin- และ USDT-margined"""
    parts = inst_id.split("-")
    if len(parts) < 5:
        return None
    try:
        return parts[2], float(parts[3]), parts[4]
    except ValueError:
        return None


def expiry_dt(yymmdd):
    """ออปชัน OKX หมดอายุ 08:00 UTC ของวันนั้น"""
    return datetime(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]), 8, tzinfo=timezone.utc)


def pct_rank(values, v):
    if not values:
        return None
    below = sum(1 for x in values if x < v)
    return round(100.0 * below / len(values), 1)


def funding_bucket(pct):
    if pct is None:
        return None
    if pct >= 80:
        return "hot"
    if pct >= 60:
        return "warm"
    if pct >= 40:
        return "mid"
    if pct >= 20:
        return "cool"
    return "cold"


BUCKET_TH = {"hot": "ร้อนจัด (สูงสุด 20% ของ 30 วัน)", "warm": "อุ่น (60–80%)",
             "mid": "กลางๆ (40–60%)", "cool": "เย็น (20–40%)", "cold": "เย็นจัด (ต่ำสุด 20%)"}


# ---------- คำนวณฝั่งออปชัน ----------

def option_metrics(oi_raw, sum_raw, spot, now):
    """คืน dict ของ expiry ที่ใกล้ที่สุดที่ยังไม่หมดอายุ: max pain · กำแพง · โซนตรึง · IV · กรอบ 1SD · 25ΔRR"""
    if not oi_raw or not sum_raw:
        return None

    # OI รายสัญญา (หน่วย oiCcy = เหรียญ ตรวจแล้วว่าทั้ง 2 ตลาดขนาดสัญญาเท่ากัน)
    oi = {}
    for d in oi_raw.get("data", []):
        try:
            v = float(d.get("oiCcy") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            oi[d["instId"]] = v
    if not oi:
        return None

    # greeks + IV รายสัญญา
    greeks = {}
    for d in sum_raw.get("data", []):
        greeks[d["instId"]] = d

    # เลือก expiry ที่ใกล้ที่สุดและยังไม่หมดอายุ
    by_exp = {}
    for inst, v in oi.items():
        p = parse_inst(inst)
        if not p:
            continue
        ex, k, t = p
        try:
            edt = expiry_dt(ex)
        except ValueError:
            continue
        if edt <= now:
            continue
        by_exp.setdefault(ex, {"dt": edt, "C": {}, "P": {}, "total": 0.0})
        by_exp[ex][t][k] = by_exp[ex][t].get(k, 0.0) + v
        by_exp[ex]["total"] += v
    if not by_exp:
        return None

    # ข้ามสัญญาที่เหลืออายุไม่ถึง 3 ชม. — แรงตรึงของมันกำลังจะหายไปภายในไม่กี่นาที
    # ถ้าใช้ตัวนั้น หน้าเว็บจะบอกว่า "ถูกตรึง" จากออปชันที่ใกล้หมดอายุ (เจอจริงตอน verify 10 ก.ย.)
    MIN_HOURS = 3
    ordered = sorted(by_exp, key=lambda e: by_exp[e]["dt"])
    live = [e for e in ordered if (by_exp[e]["dt"] - now).total_seconds() / 3600.0 >= MIN_HOURS]
    ex = (live or ordered)[0]
    blk = by_exp[ex]
    # สัญญาที่อายุใกล้ 30 วันที่สุด — ใช้เป็น IV30 สำหรับเทียบกับความผันผวนจริง 30 วัน
    ex30 = min(ordered, key=lambda e: abs((by_exp[e]["dt"] - now).total_seconds() / 86400.0 - 30))
    C, P = blk["C"], blk["P"]
    strikes = sorted(set(list(C.keys()) + list(P.keys())))

    # max pain — ราคาที่ทำให้คนถือออปชันเจ็บรวมมากที่สุด (ผู้ขายจ่ายน้อยที่สุด)
    best_k, best_pay = None, None
    for s in strikes:
        pay = sum(C[k] * (s - k) for k in C if s > k) + sum(P[k] * (k - s) for k in P if k > s)
        if best_pay is None or pay < best_pay:
            best_pay, best_k = pay, s

    call_wall = max(C, key=C.get) if C else None
    put_wall = max(P, key=P.get) if P else None
    call_wall_oi = round(C[call_wall], 1) if call_wall is not None else None
    put_wall_oi = round(P[put_wall], 1) if put_wall is not None else None

    # โซนตรึง — ช่วง strike ที่ gamma x OI หนาแน่นที่สุด (แรงที่ดูดราคาไว้)
    # จับคู่ด้วย (strike, ชนิด) ไม่ประกอบรหัสสัญญาเอง — กันพังเวลารูปแบบรหัสเปลี่ยน
    # ใช้ gammaBS ไม่ใช้ gamma — OKX ส่ง 2 ตลาดปนกัน (BTC-USD เหรียญ / BTC-USD_UM USDT)
    # "gamma" ฝั่งเหรียญเป็นหน่วยปรับราคา (~16) ฝั่ง UM เป็นหน่วย BS (~0.0002) → ปนกันแล้วจัดอันดับเพี้ยน
    # (11 ก.ย. 88,000 put gamma 2.38 แต่ gammaBS 0.0000013 → ขึ้นเป็นจุดสูงสุดทั้งที่ไกล +14%)
    # 1 ค่าต่อ (strike, ชนิด): เอาแถวฝั่งเหรียญก่อน ไม่มีค่อยใช้ UM — ต้องตรงกับ worker.js
    gamma_by = {}
    for inst, g in greeks.items():
        p = parse_inst(inst)
        if not p or p[0] != ex:
            continue
        try:
            gv = abs(float(g.get("gammaBS") or 0))
        except (TypeError, ValueError):
            continue
        um = "_UM" in inst.split("-")[1]
        key = (p[1], p[2])
        if gv and (key not in gamma_by or (gamma_by[key][1] and not um)):
            gamma_by[key] = (gv, um)
    gex = {}
    for t, book in (("C", C), ("P", P)):
        for k, v in book.items():
            g = gamma_by.get((k, t))
            if g:
                gex[k] = gex.get(k, 0.0) + g[0] * v
    # เดิม = min–max ของ 3 strike อันดับแรก → 11 ก.ย. 2026 เจออันดับ 3 เป็น 88,000 (+14% จากราคา)
    # โซนกว้าง 12,000 จุดใช้ไม่ได้ และอันดับ 3 สลับได้ในไม่กี่นาที
    # ใหม่ = เริ่มที่จุดสูงสุด ขยายไปทีละ strike ที่ติดกัน (strike ไม่มี gex = 0 → กระโดดข้ามช่องว่างไม่ได้)
    pin_zone = pin_band = None
    if gex:
        peak = max(gex, key=gex.get)
        thr = gex[peak] * PIN_KEEP
        lo = hi = strikes.index(peak)
        while lo - 1 >= 0 and gex.get(strikes[lo - 1], 0.0) >= thr:
            lo -= 1
        while hi + 1 < len(strikes) and gex.get(strikes[hi + 1], 0.0) >= thr:
            hi += 1
        pin_zone = [strikes[lo], strikes[hi]]
        # pin_band = ช่วงที่นับว่า "อยู่ในโซน" · เหลือ strike เดียว → ± ครึ่งระยะถึง strike ข้างที่ใกล้สุด
        # (11 ก.ย. ETH ได้ 2,475–2,475 → ราคาต้องเท่ากับ 2,475 พอดีถึงจะนับว่าอยู่ในโซน + แจ้งเตือนเด้งทุกครั้งที่ข้ามเส้น)
        if lo == hi:
            gaps = ([strikes[lo] - strikes[lo - 1]] if lo > 0 else []) + ([strikes[hi + 1] - strikes[hi]] if hi + 1 < len(strikes) else [])
            half = min(gaps) / 2.0 if gaps else 0.0
            pin_band = [strikes[lo] - half, strikes[lo] + half]
        else:
            pin_band = list(pin_zone)

    # IV ที่ราคาปัจจุบัน (ATM) + กรอบ 1SD ถึงวันหมดอายุ
    atm_iv, rr25 = None, None
    cands = []
    for inst, g in greeks.items():
        p = parse_inst(inst)
        if not p or p[0] != ex:
            continue
        try:
            iv = float(g.get("markVol") or 0)
            dl = float(g.get("delta") or 0)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            cands.append((abs(p[1] - spot), p[1], p[2], iv, dl))
    if cands:
        cands.sort()
        atm_iv = round(cands[0][3] * 100, 1)
        # 25 delta risk reversal — call แพงกว่า put เท่าไร (บวก = ตลาดกลัวพลาดขาขึ้น)
        calls = [(abs(d - 0.25), iv) for _, _, t, iv, d in cands if t == "C" and d > 0]
        puts = [(abs(d + 0.25), iv) for _, _, t, iv, d in cands if t == "P" and d < 0]
        if calls and puts:
            rr25 = round((min(calls)[1] - min(puts)[1]) * 100, 2)

    # IV30 — IV ที่ราคาปัจจุบันของสัญญาอายุใกล้ 30 วัน (ตัวที่เทียบกับความผันผวนจริง 30 วันได้อย่างยุติธรรม)
    # + 25ΔRR ของสัญญาเดียวกัน — ตัวใกล้หมดอายุ (rr25 ด้านบน) delta เพี้ยนแรง ETH เคยออก +14 จุด ใช้เป็นบริบทไม่ได้
    #   delta ใช้ deltaBS (Black-Scholes) ถ้ามี · delta ปกติของ OKX เป็นแบบหักค่าออปชัน (PA) ของสัญญาที่วางเหรียญเป็นหลักประกัน
    iv30, rr25_30 = None, None
    c30, calls30, puts30 = [], [], []
    for inst, g in greeks.items():
        p = parse_inst(inst)
        if not p or p[0] != ex30:
            continue
        try:
            iv = float(g.get("markVol") or 0)
            dl = float(g.get("deltaBS") or g.get("delta") or 0)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            c30.append((abs(p[1] - spot), iv))
            if p[2] == "C" and dl > 0:
                calls30.append((abs(dl - 0.25), iv))
            elif p[2] == "P" and dl < 0:
                puts30.append((abs(dl + 0.25), iv))
    if c30:
        iv30 = round(min(c30)[1] * 100, 1)
    if calls30 and puts30:
        rr25_30 = round((min(calls30)[1] - min(puts30)[1]) * 100, 2)

    hours_left = (blk["dt"] - now).total_seconds() / 3600.0
    sd1 = None
    if atm_iv and hours_left > 0:
        sd1 = spot * (atm_iv / 100.0) * math.sqrt(hours_left / (365 * 24))

    return {
        "expiry": ex,
        "expiry_utc": blk["dt"].isoformat(),
        "hours_left": round(hours_left, 1),
        "oi_coins": round(blk["total"], 1),
        "max_pain": best_k,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_oi": call_wall_oi,
        "put_wall_oi": put_wall_oi,
        "pin_zone": pin_zone,
        "pin_band": pin_band,
        "atm_iv": atm_iv,
        "iv30": iv30,
        "iv30_expiry": ex30,
        "rr25": rr25,
        "rr25_30": rr25_30,
        "sd1_move": round(sd1, 1) if sd1 else None,
        "sd1_range": [round(spot - sd1, 1), round(spot + sd1, 1)] if sd1 else None,
    }


def ema(vals, n):
    """EMA มาตรฐาน (เริ่มจาก SMA n ตัวแรก) · ข้อมูลยาวกว่า n หลายเท่า ค่าเริ่มต้นถึงจะจางจนไม่มีผล"""
    if len(vals) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def realized_vol_and_atr(symbol):
    """ความผันผวนที่เกิดขึ้นจริง 30 วัน + ATR ปัจจุบันอยู่เปอร์เซ็นไทล์ไหนของ 1 ปี + EMA + กรอบของวันนี้ (klines ย้อนหลังได้เสมอ)"""
    try:
        # 1000 วัน (เดิม 400) — EMA200 ต้องมีข้อมูลก่อนหน้ายาวพอ ค่าถึงจะตรงกับกราฟทั่วไป
        kl = fetch_json("https://data-api.binance.vision/api/v3/klines"
                        "?symbol=%sUSDT&interval=1d&limit=1000" % symbol)
    except Exception as e:
        print("ดึง klines ไม่ได้: %s" % e, file=sys.stderr)
        return {}
    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    r30 = rets[-30:]
    mean = sum(r30) / len(r30)
    rv30 = math.sqrt(sum((x - mean) ** 2 for x in r30) / (len(r30) - 1)) * math.sqrt(365) * 100

    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atrs = [sum(trs[i - 14:i]) / 14 for i in range(14, len(trs) + 1)]
    atr_now = atrs[-1]
    atr_pct_of_price = 100.0 * atr_now / closes[-1]

    # แท่งวันนี้ยังไม่ปิด → EMA และ ATR ที่ใช้วาดกรอบ ใช้เฉพาะวันที่ปิดแล้ว ค่าจะนิ่งทั้งวัน ไม่ไหลตามราคา
    done = closes[:-1]
    e30, e200 = ema(done, 30), ema(done, 200)
    atr_prev = atrs[-2] if len(atrs) >= 2 else atr_now
    return {
        "rv30": round(rv30, 1),
        "atr14": round(atr_now, 1),
        "atr_pct": round(atr_pct_of_price, 2),
        "atr_percentile_1y": pct_rank(atrs[-365:], atr_now),
        "atr_n": len(atrs[-365:]),
        "atr14_prev": round(atr_prev, 1),
        "ema30": round(e30, 1) if e30 else None,
        "ema200": round(e200, 1) if e200 else None,
        "day_open": float(kl[-1][1]),   # แท่งวันเริ่ม 00:00 UTC = 07:00 น. ไทย
        "day_high": highs[-1],
        "day_low": lows[-1],
    }


# ---------- ประกอบเป็น "ต้องทำอะไร" ----------

def build_checks(sym, spot, opt, fund, rv, prev):
    """แต่ละข้อ: ข้อเท็จจริง + แปลว่าอะไร + ระดับหลักฐาน (proven / weak / none)"""
    checks = []

    # 1. ราคาอยู่ตรงไหนเทียบโซนตรึง
    if opt and opt.get("pin_zone"):
        zl, zh = opt["pin_zone"]
        lo, hi = opt.get("pin_band") or opt["pin_zone"]   # strike เดียว = ช่วง ± ครึ่งระยะ strike
        inside = lo <= spot <= hi
        zone_txt = ("จุดตรึง %s (±%s)" % (f"{zl:,.0f}", f"{hi - zl:,.1f}".rstrip("0").rstrip(".")) if zl == zh
                    else "โซนตรึง %s–%s" % (f"{zl:,.0f}", f"{zh:,.0f}"))
        checks.append({
            "key": "pin_zone",
            "title": "ราคาเทียบโซนที่ออปชันตรึงไว้",
            "fact": "ราคา %s · %s" % (f"{spot:,.0f}", zone_txt),
            "state": "in" if inside else ("above" if spot > hi else "below"),
            "means": ("อยู่ในโซน — แรงจากออปชันมักดึงราคากลับเข้ากลาง กว่าจะไปต่อต้องแรงกว่าปกติ"
                      if inside else "อยู่นอกโซน — แรงตรึงอ่อนลง ราคาวิ่งได้อิสระกว่า"),
            "evidence": "none",
            "evidence_note": "ยังไม่มีสถิติของตัวเอง — เพิ่งเริ่มเก็บ 10 ก.ย. 2026 · ต้องสะสม 4–8 สัปดาห์ถึงจะบอกได้ว่าโซนนี้ตรึงจริงกี่ %",
        })

    # 2. ราคาเทียบกรอบ 1SD ถึงวันหมดอายุ
    if opt and opt.get("sd1_range"):
        lo, hi = opt["sd1_range"]
        checks.append({
            "key": "sd1",
            "title": "กรอบที่ตลาดออปชันคิดว่าจะวิ่งถึงหมดอายุ (%s ชม.)" % opt["hours_left"],
            "fact": "กรอบ %s–%s (±%s)" % (f"{lo:,.0f}", f"{hi:,.0f}", f"{opt['sd1_move']:,.0f}"),
            "state": "info",
            "means": "หลุดกรอบนี้ = ตลาดเคลื่อนแรงกว่าที่ออปชันตั้งราคาไว้ · ใช้ตั้งความคาดหวังระยะสั้น ไม่ใช่สัญญาณเข้า",
            "evidence": "weak",
            "evidence_note": "กรอบคำนวณจากสูตรมาตรฐาน (IV × รากเวลา) — ยังไม่ได้ทดสอบย้อนหลังว่าราคาอยู่ในกรอบจริงกี่ % เพราะ IV ย้อนหลังไม่ได้",
        })

    # 3. funding — ข้อเดียวที่มีสถิติของจริงรองรับ
    if fund and fund.get("percentile") is not None:
        b = fund["bucket"]
        ev = FUNDING_EVIDENCE.get(sym, {})
        bk = ev.get("buckets", {}).get(b, {})
        base = ev.get("baseline_up8h")
        proven = bool(bk.get("stable")) and bk.get("n", 0) >= 30
        diff = (bk.get("up8h") - base) if (bk.get("up8h") is not None and base) else None
        means = "ยังไม่พบว่าสภาพ funding แบบนี้บอกทิศทางอะไรได้ในข้อมูลที่มี"
        if proven and diff is not None:
            lean = "ขึ้น" if diff > 0 else "ลง"
            means = ("ในอดีตเมื่อ funding อยู่ระดับนี้ ราคาปิดสูงขึ้นใน 8 ชม.ถัดไป %s%% ของครั้ง (ปกติ %s%%) "
                     "→ เอียงไปทาง%sบ่อยกว่าปกติ %s จุด · ผลนี้ยืนได้ทั้งครึ่งแรกและครึ่งหลังของตัวอย่าง"
                     % (bk["up8h"], base, lean, round(abs(diff), 1)))
        checks.append({
            "key": "funding",
            "title": "ต้นทุนถือสถานะ (funding) เทียบตัวเองย้อน 30 วัน",
            "fact": "รอบนี้ %.4f%% · อยู่ที่เปอร์เซ็นไทล์ %s → %s" % (
                fund["rate_pct"], fund["percentile"], BUCKET_TH.get(b, b)),
            "state": b,
            "means": means,
            "evidence": "proven" if proven else "weak",
            "evidence_note": ("จากข้อมูลจริง %s · n=%s ครั้ง · ตรวจซ้ำด้วยการแบ่งครึ่งตัวอย่าง (%s%% / %s%%)"
                              % (ev.get("window"), bk.get("n"), *(bk.get("halves") or [None, None]))
                              if proven else
                              "n=%s ครั้ง แต่ผลไม่คงเส้นคงวาเมื่อแบ่งครึ่งตัวอย่าง → ใช้ประกอบเท่านั้น ห้ามใช้เดี่ยว"
                              % bk.get("n")),
        })

    # 4. ออปชันถูกหรือแพงเทียบความผันผวนที่เกิดขึ้นจริง
    if opt and opt.get("iv30") and rv.get("rv30"):
        gap = round(opt["iv30"] - rv["rv30"], 1)
        checks.append({
            "key": "iv_vs_rv",
            "title": "ออปชันถูกหรือแพง (IV สัญญาอายุ ~30 วัน เทียบความผันผวนจริง 30 วัน)",
            "fact": "IV30 %s%% (สัญญา %s) vs ผันผวนจริง %s%% → %s %s จุด" % (
                opt["iv30"], opt.get("iv30_expiry"), rv["rv30"], "แพงกว่า" if gap > 0 else "ถูกกว่า", abs(gap)),
            "state": "expensive" if gap > 0 else "cheap",
            "means": ("ออปชันแพงกว่าความผันผวนที่เกิดจริง — ฝั่งขายออปชันได้เปรียบเชิงสถิติ"
                      if gap > 0 else
                      "ออปชันถูกกว่าความผันผวนที่เกิดจริง — ซื้อความคุ้มครอง/เก็งกำไรความผันผวนคุ้มกว่าขาย"),
            "evidence": "weak",
            "evidence_note": "เป็นการเทียบตัวเลข 2 ตัวตรงๆ ไม่ใช่ผล backtest — ยังไม่ได้พิสูจน์ว่าเทรดตามนี้แล้วกำไรจริงหลังหักต้นทุน",
        })

    # 5. ความแรงของตลาดตอนนี้เทียบ 1 ปี
    if rv.get("atr_percentile_1y") is not None:
        p = rv["atr_percentile_1y"]
        checks.append({
            "key": "atr",
            "title": "ตลาดตอนนี้เหวี่ยงแรงแค่ไหนเทียบ 1 ปี",
            "fact": "ช่วงแกว่งต่อวัน %s%% ของราคา · อยู่เปอร์เซ็นไทล์ %s ของปีที่ผ่านมา" % (rv["atr_pct"], p),
            "state": "high" if p >= 70 else ("low" if p <= 30 else "normal"),
            "means": ("เหวี่ยงแรงกว่าปกติ — ระยะ stop เดิมจะโดนกินง่ายขึ้น ต้องลดขนาดสถานะลงถ้าจะใช้ระยะเท่าเดิม"
                      if p >= 70 else
                      "เหวี่ยงน้อยกว่าปกติ — ระยะ stop กว้างเกินจะเสียโอกาส แต่ระวังช่วงเงียบมักจบด้วยการระเบิดออกข้าง"
                      if p <= 30 else "แกว่งระดับปกติของปีนี้"),
            "evidence": "proven",
            "evidence_note": "คำนวณจากราคาจริงย้อนหลัง %s วัน (ข้อมูลราคาย้อนหลังได้เต็ม ไม่ต้องรอสะสม)" % rv.get("atr_n"),
        })

    # 6. สัญญาณขัดกัน — funding กับ OI เดินคนละทาง
    if prev and fund and fund.get("oi_usd") and prev.get("funding", {}).get("oi_usd"):
        d_oi = 100.0 * (fund["oi_usd"] - prev["funding"]["oi_usd"]) / prev["funding"]["oi_usd"]
        d_f = fund["rate_pct"] - prev["funding"]["rate_pct"]
        if abs(d_oi) >= 1.0 and abs(d_f) > 0.0005:
            conflict = (d_f > 0 and d_oi < 0) or (d_f < 0 and d_oi > 0)
            checks.append({
                "key": "funding_vs_oi",
                "title": "funding กับสถานะค้างในตลาด เดินทางเดียวกันไหม",
                "fact": "funding %s%.4f จุด · สถานะค้าง %s%.1f%%" % (
                    "+" if d_f > 0 else "", d_f, "+" if d_oi > 0 else "", d_oi),
                "state": "conflict" if conflict else "aligned",
                "means": ("ขัดกัน — คนที่เหลืออยู่ยอมจ่ายแพงขึ้นแต่ไม่มีเงินใหม่เข้า มักเป็นภาพของการไล่ราคาที่ไม่มีคนหนุน"
                          if conflict else "ไปทางเดียวกัน — มีเงินใหม่เข้าหนุนทิศทางนี้จริง"),
                "evidence": "none",
                "evidence_note": "ยังไม่ได้ทดสอบย้อนหลัง — OKX ให้ประวัติสถานะค้างแค่ 30 วัน ต้องสะสมเองก่อน",
            })
    return checks


def build_bias(sym, spot, opt, fund, rv):
    """bias ทิศทาง — แยกข้อที่มีหลักฐานออกจากบริบทเด็ดขาด

    proven  = ข้อที่ผ่าน backtest + แบ่งครึ่งตัวอย่างแล้วยืน (ตอนนี้มีแค่ funding ของ BTC)
    context = ข้อเท็จจริงที่ชี้ทิศได้ แต่ยังไม่มีสถิติ → หน้าเว็บห้ามนับรวมเป็นสัญญาณ ใช้ดูว่าหนุนหรือขัดเท่านั้น
    ไม่มีข้อ proven = บอกตรงๆ ว่าไม่มี bias ห้ามเอาบริบทมานับคะแนนแทน
    """
    proven = None
    if fund and fund.get("bucket"):
        ev = FUNDING_EVIDENCE.get(sym, {})
        bk = ev.get("buckets", {}).get(fund["bucket"], {})
        base = ev.get("baseline_up8h")
        if bk.get("stable") and bk.get("n", 0) >= 30 and base:
            diff = round(bk["up8h"] - base, 1)
            if abs(diff) >= 5:   # ต่างจากปกติไม่ถึง 5 จุด = เล็กเกินจะเรียกว่าเอียง
                proven = {
                    "dir": "up" if diff > 0 else "down",
                    "horizon_h": 8,
                    "why": "funding %s" % BUCKET_TH.get(fund["bucket"], fund["bucket"]),
                    "stat": "ในอดีตราคาปิดสูงขึ้นใน 8 ชม.ถัดไป %s%% ของครั้ง (ปกติ %s%%)" % (bk["up8h"], base),
                    "up8h": bk["up8h"], "base": base,
                    "edge": diff, "n": bk["n"], "halves": bk.get("halves"), "window": ev.get("window"),
                }

    ctx = []
    if opt and opt.get("max_pain"):
        mp = opt["max_pain"]
        gap = 100.0 * (mp - spot) / spot
        ctx.append({
            "name": "แรงดูดเข้า max pain",
            "dir": "up" if gap > 0.3 else ("down" if gap < -0.3 else "flat"),
            "fact": "max pain %s (%+.1f%%) · ออปชันชุดนี้หมดอายุอีก %s ชม." % (f"{mp:,.0f}", gap, opt.get("hours_left")),
        })
    rr = opt.get("rr25_30") if opt else None
    if rr is not None:
        ctx.append({
            "name": "ราคา call เทียบ put (25ΔRR สัญญา ~30 วัน)",
            "dir": "up" if rr >= 2 else ("down" if rr <= -2 else "flat"),
            "fact": "%+.1f จุด · บวก = ตลาดยอมจ่ายแพงกว่าเพื่อเก็งขึ้น · ใกล้ 0 = ไม่เอียง" % rr,
        })
    e30, e200 = rv.get("ema30"), rv.get("ema200")
    if e30 and e200:
        if spot > e30 > e200:
            d, f = "up", "ราคา > EMA30 > EMA200 — ขาขึ้นเรียงตัว"
        elif spot < e30 < e200:
            d, f = "down", "ราคา < EMA30 < EMA200 — ขาลงเรียงตัว"
        else:
            d, f = "flat", "เส้นไม่เรียงตัว — แนวโน้มไม่ชัด"
        ctx.append({"name": "แนวโน้ม EMA (1D)", "dir": d,
                    "fact": "%s · EMA30 %s · EMA200 %s" % (f, f"{e30:,.0f}", f"{e200:,.0f}")})
    for c in ctx:
        c["evidence"] = "none"

    return {
        "proven": proven,
        "context": ctx,
        "ctx_up": sum(1 for c in ctx if c["dir"] == "up"),
        "ctx_down": sum(1 for c in ctx if c["dir"] == "down"),
    }


def decide_mode(sym, spot, opt, checks):
    """สรุปโหมดตลาดจากสิ่งที่ตรวจได้จริง — เขียนให้อ่านแล้วรู้ว่ากำลังเจอสภาพแบบไหน"""
    inside_pin = any(c["key"] == "pin_zone" and c["state"] == "in" for c in checks)
    atr_state = next((c["state"] for c in checks if c["key"] == "atr"), "normal")
    if inside_pin and atr_state != "high":
        return {"mode": "ถูกตรึง", "icon": "🧲",
                "why": "ราคาอยู่ในโซนที่ออปชันหนาแน่น และตลาดไม่ได้เหวี่ยงแรงกว่าปกติ",
                "do": "รอให้หลุดกรอบก่อนค่อยไล่ — ในโซนแบบนี้การไล่ราคามักโดนดึงกลับ"}
    if atr_state == "high":
        return {"mode": "เหวี่ยงแรง", "icon": "⚡",
                "why": "ช่วงแกว่งต่อวันอยู่ในกลุ่มสูงของปีนี้",
                "do": "ลดขนาดสถานะ หรือถ้าจะคงขนาดต้องขยายระยะ stop ตามความแรงที่เพิ่มขึ้น"}
    if atr_state == "low":
        return {"mode": "เงียบผิดปกติ", "icon": "😴",
                "why": "ช่วงแกว่งต่อวันอยู่ในกลุ่มต่ำของปีนี้",
                "do": "ระวังการทะลุออกข้างแบบเร็ว — ช่วงเงียบมักจบด้วยการเคลื่อนแรงครั้งเดียว"}
    return {"mode": "ปกติ", "icon": "➖",
            "why": "ไม่มีสภาพเด่นที่ตรวจพบจากข้อมูลชุดนี้",
            "do": "ไม่มีอะไรพิเศษให้ทำจากข้อมูลชุดนี้ — ใช้เกณฑ์ปกติของผู้ใช้เอง"}


def load_history():
    """ดึงประวัติค่าที่คำนวณแล้วจาก repo เพื่อทำเส้นเวลาและ delta

    คืน [] ถ้ายังไม่มีไฟล์ (รอบแรก) · คืน None ถ้าดึงพลาดด้วยเหตุอื่น → main จะไม่เขียนทับประวัติรอบนั้น
    (เดิมพลาดแล้วคืน [] = เขียนทับประวัติทั้งหมดเหลือจุดเดียว · เจอตอน verify 10 ก.ย.)
    ใช้ API แทน raw.githubusercontent เพราะตัวหลังมีแคช ~5 นาที
    """
    url = "https://api.github.com/repos/%s/contents/web/history.json?ref=main" % REPO
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github.raw"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer %s" % tok)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, list) else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        print("โหลด history ไม่ได้ (HTTP %s) — รอบนี้จะไม่เขียนทับประวัติ" % e.code, file=sys.stderr)
        return None
    except Exception as e:
        print("โหลด history ไม่ได้ (%s) — รอบนี้จะไม่เขียนทับประวัติ" % e, file=sys.stderr)
        return None


def main():
    now = datetime.now(timezone.utc)
    snap = newest_snapshot_dir()
    print("snapshot: %s" % snap)

    history = load_history()
    history_ok = history is not None
    if not history_ok:
        history = []

    out = {"generated_at_utc": now.isoformat(), "snapshot": snap.name, "symbols": {}}

    for sym in ("BTC", "ETH"):
        low = sym.lower()
        tick = load(snap, "okx_ticker_swap_%s" % low)
        idx = load(snap, "okx_index_%s" % low)
        bn = load(snap, "binance_ticker24h_%s" % low)
        f_now = load(snap, "okx_funding_%s" % low)
        f_hist = load(snap, "okx_funding_hist_%s" % low)
        oi_swap = load(snap, "okx_oi_swap_%s" % low)
        opt_oi = load(snap, "okx_opt_oi_%s" % low)
        opt_sum = load(snap, "okx_optsum_%s" % low)

        spot = None
        for src in (tick, idx):
            try:
                spot = float(src["data"][0].get("last") or src["data"][0].get("idxPx"))
                break
            except Exception:
                continue
        if spot is None and bn:
            spot = float(bn.get("lastPrice", 0)) or None
        if not spot:
            print("ไม่มีราคาให้ใช้สำหรับ %s — ข้าม" % sym, file=sys.stderr)
            continue

        chg24 = float(bn.get("priceChangePercent")) if bn and bn.get("priceChangePercent") else None

        fund = None
        if f_now:
            try:
                rate = float(f_now["data"][0]["fundingRate"]) * 100
                # 90 รอบล่าสุด (30 วัน) ให้ตรงกับวิธีที่ใช้ใน backtest — OKX เรียงใหม่สุดก่อน
                hist = [float(d.get("realizedRate") or d["fundingRate"]) * 100
                        for d in (f_hist or {}).get("data", [])][:90]
                p = pct_rank(hist, rate) if len(hist) >= 20 else None
                oi_usd = None
                if oi_swap:
                    try:
                        oi_usd = float(oi_swap["data"][0]["oiUsd"])
                    except Exception:
                        oi_usd = None
                fund = {"rate_pct": round(rate, 5), "percentile": p, "bucket": funding_bucket(p),
                        "history_n": len(hist), "oi_usd": oi_usd,
                        "next_time_utc": datetime.fromtimestamp(
                            int(f_now["data"][0]["fundingTime"]) / 1000, timezone.utc).isoformat()}
            except Exception as e:
                print("funding %s: %s" % (sym, e), file=sys.stderr)

        opt = option_metrics(opt_oi, opt_sum, spot, now)
        rv = realized_vol_and_atr(sym)
        prev = next((h["symbols"][sym] for h in reversed(history)
                     if isinstance(h, dict) and sym in h.get("symbols", {})), None)
        checks = build_checks(sym, spot, opt, fund, rv, prev)
        mode = decide_mode(sym, spot, opt, checks)
        bias = build_bias(sym, spot, opt, fund, rv)

        out["symbols"][sym] = {
            "spot": spot, "change_24h": chg24, "mode": mode,
            "options": opt, "funding": fund, "vol": rv, "checks": checks, "bias": bias,
        }
        print("%s %s · โหมด %s · เช็ก %d ข้อ" % (sym, f"{spot:,.1f}", mode["mode"], len(checks)))

    # เก็บเฉพาะค่าที่ต้องใช้ทำเส้นเวลา (ไฟล์นี้ต้องเล็ก เพราะโหลดทุกครั้งที่เปิดหน้าเว็บ)
    slim = {"generated_at_utc": out["generated_at_utc"], "symbols": {}}
    for sym, d in out["symbols"].items():
        slim["symbols"][sym] = {
            "spot": d["spot"],
            "funding": {k: d["funding"][k] for k in ("rate_pct", "percentile", "oi_usd")} if d["funding"] else None,
            "options": {k: (d["options"] or {}).get(k) for k in ("max_pain", "atm_iv", "pin_zone", "pin_band", "oi_coins", "expiry")} if d["options"] else None,
            "vol": {"rv30": d["vol"].get("rv30"), "atr_pct": d["vol"].get("atr_pct")},
        }
    history.append(slim)
    cutoff = (now - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    history = [h for h in history if h.get("generated_at_utc", "") >= cutoff][-2000:]

    # ต่อเส้นเวลาให้หน้าเว็บใช้ทันที: เทียบกับ 2 / 8 / 24 ชม.ที่แล้ว
    out["timeline"] = {}
    for hours in (2, 8, 24):
        target = (now - timedelta(hours=hours)).isoformat()
        past = next((h for h in reversed(history[:-1]) if h.get("generated_at_utc", "") <= target), None)
        if past:
            out["timeline"]["%dh" % hours] = past

    webdir = OUT_DIR / "web"
    webdir.mkdir(parents=True, exist_ok=True)
    (webdir / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if history_ok:
        (webdir / "history.json").write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("เขียน web/latest.json (%d เหรียญ) · history %d จุด" % (len(out["symbols"]), len(history)))
    return 0 if out["symbols"] else 1


if __name__ == "__main__":
    sys.exit(main())
