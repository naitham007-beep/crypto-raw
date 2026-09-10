#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_github.py — ยัดไฟล์ที่ collect.py เพิ่งเก็บ เข้า repo ผ่าน Git Data API

ทำไมไม่ใช้ `git add/commit/push` ธรรมดา:
  repo จะโตขึ้นเรื่อยๆ (~1 GB/ปี) ถ้า checkout ทั้งก้อนทุก 2 ชม. จะช้าและเปลืองขึ้นทุกวัน
  Git Data API สร้าง blob/tree/commit ตรงๆ ไม่ต้องมีสำเนา repo ในเครื่องรันเลย = เวลารันคงที่ตลอดไป

ใช้ stdlib ล้วน · ต้องมี env: GITHUB_TOKEN, GITHUB_REPOSITORY (owner/repo)
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME") or "main"
OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))


def api(method, path, payload=None, retries=3):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer %s" % TOKEN,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "crypto-raw-collector/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last = RuntimeError("%s %s -> %s %s" % (method, url, e.code, detail))
            if e.code in (401, 403, 404, 422):   # ยิงซ้ำก็ไม่หาย
                raise last
        except Exception as e:
            last = e
        if attempt < retries:
            time.sleep(3 * attempt)
    raise last


def main():
    if not TOKEN or not REPO:
        print("ERROR: ต้องมี GITHUB_TOKEN และ GITHUB_REPOSITORY", file=sys.stderr)
        return 1

    files = sorted(p for p in OUT_DIR.rglob("*") if p.is_file())
    if not files:
        print("ERROR: ไม่มีไฟล์ให้ push (collect.py ทำงานหรือยัง?)", file=sys.stderr)
        return 1

    meta_path = next((p for p in files if p.name == "_meta.json"), None)
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path else {}

    # 1) blob ทีละไฟล์ (base64 = ปลอดภัยกับไฟล์บีบอัด)
    tree_items = []
    total = 0
    for p in files:
        raw = p.read_bytes()
        total += len(raw)
        blob = api("POST", "/repos/%s/git/blobs" % REPO, {
            "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
        rel = p.relative_to(OUT_DIR).as_posix()
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("blob  %-52s %7d B" % (rel, len(raw)), flush=True)

    # 2) สรุปสถานะล่าสุด — ไฟล์เดียวที่ทับของเดิม ไว้เปิดดูเร็วๆ ว่าตัวเก็บยังไหวอยู่มั้ย
    if meta:
        status = {
            "last_run_utc": meta.get("collected_at_utc"),
            "stamp": meta.get("stamp"),
            "ok": "%d/%d" % (meta.get("n_ok", 0), meta.get("n_endpoints", 0)),
            "failed": [e["name"] for e in meta.get("endpoints", []) if not e.get("ok")],
            "bytes_xz_this_run": meta.get("total_bytes_xz"),
            "run_url": "https://github.com/%s/actions/runs/%s" % (REPO, meta.get("run_id")),
        }
        sblob = api("POST", "/repos/%s/git/blobs" % REPO, {
            "content": json.dumps(status, ensure_ascii=False, indent=1), "encoding": "utf-8"})
        tree_items.append({"path": "status/last_run.json", "mode": "100644",
                           "type": "blob", "sha": sblob["sha"]})

    # 3) commit ต่อยอดจากปลายกิ่ง (ลองใหม่ได้ถ้ามีคนแทรกระหว่างทาง)
    msg = "snapshot %s (ok %s/%s)" % (meta.get("stamp", "?"),
                                      meta.get("n_ok", "?"), meta.get("n_endpoints", "?"))
    for attempt in range(1, 4):
        ref = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
        parent = ref["object"]["sha"]
        base_tree = api("GET", "/repos/%s/git/commits/%s" % (REPO, parent))["tree"]["sha"]
        tree = api("POST", "/repos/%s/git/trees" % REPO,
                   {"base_tree": base_tree, "tree": tree_items})
        commit = api("POST", "/repos/%s/git/commits" % REPO,
                     {"message": msg, "tree": tree["sha"], "parents": [parent]})
        try:
            api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
                {"sha": commit["sha"], "force": False})
            print("\npushed %s · %d ไฟล์ · %.0f KB · commit %s" % (
                meta.get("stamp", "?"), len(tree_items), total / 1024, commit["sha"][:7]))
            return 0
        except Exception as e:
            print("push ชนกับ commit อื่น (รอบ %d): %s" % (attempt, e), file=sys.stderr)
            time.sleep(4 * attempt)
    print("ERROR: push ไม่สำเร็จหลังลอง 3 รอบ", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
