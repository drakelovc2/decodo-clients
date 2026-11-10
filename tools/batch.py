#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Последовательный батч Decodo: N аккаунтов с паузой, банк после каждого.

    python3 tools/batch.py --count 5 --pause 90
    python3 tools/batch.py --count 5 --proxy http://user:pass@host:port

Пишет data/accounts.jsonl (results.save_account) и лог data/logs/batch.log.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import results                      # noqa: E402
from core.register import register_one        # noqa: E402

LOG_DIR = os.path.join(ROOT, "data", "logs")


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "batch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--pause", type=int, default=90, help="секунд между аккаунтами")
    ap.add_argument("--proxy", default=None, help="один прокси на все аккаунты")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()

    ok = fail = 0
    for i in range(1, args.count + 1):
        log(f"── аккаунт {i}/{args.count} ──")
        t0 = time.time()
        try:
            rec = register_one(proxy=args.proxy,
                               on_step=lambda m: log(f"   · {m}"))
            results.save_account(rec)
            ok += 1
            log(f"   ✓ {rec.get('email')} | {rec.get('proxy_username')} | "
                f"{rec.get('proxy_check')} | {int(time.time() - t0)}s")
        except Exception as exc:
            fail += 1
            log(f"   ✗ {type(exc).__name__}: {str(exc)[:200]} | {int(time.time() - t0)}s")
        if i < args.count:
            log(f"   пауза {args.pause}s")
            time.sleep(args.pause)
    log(f"ИТОГО: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
