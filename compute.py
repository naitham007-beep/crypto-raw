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
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
REPO = os.environ.get("GITHUB_REPOSITORY", "naitham007-beep/crypto-raw")
HISTORY_KEEP_DAYS = 120
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

    ex = sorted(by_exp, key=lambda e: by_exp[e]["dt"])[0]
    blk = by_exp[ex]
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

    # โซนตรึง — ช่วง strike ที่ gamma x OI หนาแน่นที่สุด (แรงที่ดูดราคาไว้)
    # จับคู่ด้วย (strike, ชนิด) ไม่ประกอบรหัสสัญญาเอง — กันพังเวลารูปแบบรหัสเปลี่ยน
    gamma_by = {}
    for inst, g in greeks.items():
        p = parse_inst(inst)
        if not p or p[0] != ex:
            continue
        try:
            gv = float(g.get("gamma") or 0)
        except (TypeError, ValueError):
            continue
        if gv:
            gamma_by.setdefault((p[1], p[2]), gv)
    gex = {}
    for t, book in (("C", C), ("P", P)):
        for k, v in book.items():
            g = gamma_by.get((k, t))
            if g:
                gex[k] = gex.get(k, 0.0) + abs(g) * v
    pin_zone = None
    if gex:
        top = sorted(gex, key=gex.get, reverse=True)[:3]
        pin_zone = [min(top), max(top)]

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
        "pin_zone": pin_zone,
        "atm_iv": atm_iv,
        "rr25": rr25,
        "sd1_move": round(sd1, 1) if sd1 else None,
        "sd1_range": [round(spot - sd1, 1), round(spot + sd1, 1)] if sd1 else None,
    }


def realized_vol_and_atr(symbol):
    """ความผันผวนที่เกิดขึ้นจริง 30 วัน + ATR ปัจจุบันอยู่เปอร์เซ็นไทล์ไหนของ 1 ปี (klines ย้อนหลังได้เสมอ)"""
    try:
        kl = fetch_json("https://data-api.binance.vision/api/v3/klines"
                        "?symbol=%sUSDT&interval=1d&limit=400" % symbol)
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
    return {
        "rv30": round(rv30, 1),
        "atr14": round(atr_now, 1),
        "atr_pct": round(atr_pct_of_price, 2),
        "atr_percentile_1y": pct_rank(atrs[-365:], atr_now),
        "atr_n": len(atrs[-365:]),
    }


# ---------- ประกอบเป็น "ต้องทำอะไร" ----------

def build_checks(sym, spot, opt, fund, rv, prev):
    """แต่ละข้อ: ข้อเท็จจริง + แปลว่าอะไร + ระดับหลักฐาน (proven / weak / none)"""
    checks = []

    # 1. ราคาอยู่ตรงไหนเทียบโซนตรึง
    if opt and opt.get("pin_zone"):
        lo, hi = opt["pin_zone"]
        inside = lo <= spot <= hi
        checks.append({
            "key": "pin_zone",
            "title": "ราคาเทียบโซนที่ออปชันตรึงไว้",
            "fact": "ราคา %s · โซนตรึง %s–%s" % (f"{spot:,.0f}", f"{lo:,.0f}", f"{hi:,.0f}"),
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
            direction = "ขึ้น" if diff > 0 else "ลง"
            means = ("ในอดีต funding แบบนี้ ราคา 8 ชม.ถัดไป%s %s%% (ปกติ %s%%) — ต่างจากปกติ %s จุด "
                     "และผลนี้ยืนได้ทั้งครึ่งแรกและครึ่งหลังของตัวอย่าง"
                     % (direction, bk["up8h"], base, round(abs(diff), 1)))
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
    if opt and opt.get("atm_iv") and rv.get("rv30"):
        gap = round(opt["atm_iv"] - rv["rv30"], 1)
        checks.append({
            "key": "iv_vs_rv",
            "title": "ออปชันถูกหรือแพง (IV เทียบความผันผวนจริง 30 วัน)",
            "fact": "IV %s%% vs ผันผวนจริง %s%% → %s %s จุด" % (
                opt["atm_iv"], rv["rv30"], "แพงกว่า" if gap > 0 else "ถูกกว่า", abs(gap)),
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
            "do": "ไม่มีอะไรพิเศษให้ทำจากข้อมูลชุดนี้ — ใช้เกณฑ์ปกติของ MD เอง"}


def load_history():
    """ดึงประวัติค่าที่คำนวณแล้วจาก repo (ถ้ามี) เพื่อทำเส้นเวลาและ delta"""
    url = "https://raw.githubusercontent.com/%s/main/web/history.json" % REPO
    try:
        return fetch_json(url, timeout=20)
    except Exception:
        return []


def main():
    now = datetime.now(timezone.utc)
    snap = newest_snapshot_dir()
    print("snapshot: %s" % snap)

    history = load_history()
    if not isinstance(history, list):
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

        out["symbols"][sym] = {
            "spot": spot, "change_24h": chg24, "mode": mode,
            "options": opt, "funding": fund, "vol": rv, "checks": checks,
        }
        print("%s %s · โหมด %s · เช็ก %d ข้อ" % (sym, f"{spot:,.1f}", mode["mode"], len(checks)))

    # เก็บเฉพาะค่าที่ต้องใช้ทำเส้นเวลา (ไฟล์นี้ต้องเล็ก เพราะโหลดทุกครั้งที่เปิดหน้าเว็บ)
    slim = {"generated_at_utc": out["generated_at_utc"], "symbols": {}}
    for sym, d in out["symbols"].items():
        slim["symbols"][sym] = {
            "spot": d["spot"],
            "funding": {k: d["funding"][k] for k in ("rate_pct", "percentile", "oi_usd")} if d["funding"] else None,
            "options": {k: (d["options"] or {}).get(k) for k in ("max_pain", "atm_iv", "pin_zone", "oi_coins", "expiry")} if d["options"] else None,
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
    (webdir / "history.json").write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("เขียน web/latest.json (%d เหรียญ) · history %d จุด" % (len(out["symbols"]), len(history)))
    return 0 if out["symbols"] else 1


if __name__ == "__main__":
    sys.exit(main())
