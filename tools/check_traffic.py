# -*- coding: utf-8 -*-
"""check_traffic.py — остаток GB по каждому аккаунту Decodo (free_dc).

Читает data/accounts.jsonl, для каждого аккаунта: Camoufox-логин →
GET /subscription-api/v1/subscriptions → free_dc.traffic (использовано GB)
и traffic_limit (лимит GB). Пишет data/traffic-YYYY-MM-DD-HHMM.json и печатает
сводку.

    python3 tools/check_traffic.py [--only 3] [--accounts data/accounts.jsonl]
"""
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JS_FETCH = """async ({url,method,body}) => {
  const r = await fetch(url,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined,credentials:'include'});
  const text = await r.text(); return {status:r.status, body:text};
}"""

def wait_cf(page, limit=150):
    d = time.time() + limit
    while time.time() < d:
        t = (page.title() or "").lower()
        if "just a moment" not in t and "security verification" not in t:
            return True
        time.sleep(2)
    return False

def fetch(page, url, method="GET", body=None):
    r = page.evaluate(JS_FETCH, {"url": url, "method": method, "body": body})
    return r["status"], r["body"]

def check_one(email, pw):
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=True, humanize=True, locale="en-US", geoip=True) as browser:
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.goto("https://dashboard.decodo.com/login", wait_until="domcontentloaded", timeout=90000)
        if not wait_cf(page):
            return {"error": "CF challenge"}
        time.sleep(3)
        page.fill("#email", email)
        page.fill("#password", pw)
        time.sleep(1)
        for bt in page.query_selector_all("button"):
            t = (bt.inner_text() or "").strip().lower()
            if "log in" in t or "sign in" in t or "continue" in t:
                bt.click(); break
        time.sleep(8)
        st, body = fetch(page, "/subscription-api/v1/subscriptions")
        if st != 200:
            return {"error": f"subs http {st}"}
        j = json.loads(body)
        sub = (j or {}).get("free_dc")
        if not sub:
            return {"error": "no free_dc"}
        limit = sub.get("traffic_limit")
        used = sub.get("traffic")
        return {"uuid": sub.get("uuid"), "traffic_limit_gb": limit,
                "traffic_used_gb": used,
                "traffic_left_gb": round((limit or 0) - (used or 0), 3),
                "valid_until": sub.get("valid_until"), "status": sub.get("status")}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default=str(ROOT / "data" / "accounts.jsonl"))
    ap.add_argument("--only", type=int, default=0, help="первые N аккаунтов (0 = все)")
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(args.accounts, encoding="utf-8") if l.strip()]
    if args.only:
        rows = rows[:args.only]
    out = []
    total_left = 0.0
    for i, rec in enumerate(rows, 1):
        email, pw = rec.get("email"), rec.get("password")
        if not (email and pw):
            out.append({"email": email, "error": "no creds"}); continue
        print(f"[{i}/{len(rows)}] {email} ...", flush=True)
        try:
            r = check_one(email, pw)
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {str(e)[:140]}"}
        r["email"] = email
        out.append(r)
        if r.get("traffic_left_gb") is not None:
            total_left += r["traffic_left_gb"]
            print(f"  → left {r['traffic_left_gb']} GB (used {r.get('traffic_used_gb')}/{r.get('traffic_limit_gb')})")
        else:
            print(f"  → {r.get('error')}")
    ts = time.strftime("%Y-%m-%d-%H%M")
    path = ROOT / "data" / f"traffic-{ts}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = [r for r in out if "traffic_left_gb" in r]
    print(f"\nГОТОВО: {len(ok)}/{len(rows)} аккаунтов, суммарно свободно {round(total_left,2)} GB")
    print(f"сводка: {path}")

if __name__ == "__main__":
    main()
