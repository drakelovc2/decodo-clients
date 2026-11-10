#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка пула Decodo: все аккаунты из банка → живой прокси + exit IP.

    python3 tools/check_pool.py            # проверить всё
    python3 tools/check_pool.py --json     # машинный вывод
    python3 tools/check_pool.py --update   # обновить proxy_ok/exit_ip в банке

Параллельно (--workers 8), один запрос ipinfo на аккаунт.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import results                    # noqa: E402
from core.register import test_proxy        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()

    accs = results.load_accounts()
    if not accs:
        print("банк пуст: data/accounts.jsonl")
        return

    def check(acc):
        ok, detail = test_proxy(acc)
        return acc, ok, detail

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for acc, ok, detail in ex.map(check, accs):
            rows.append({"email": acc.get("email"), "username": acc.get("proxy_username"),
                         "ok": ok, "detail": detail, "gb": acc.get("traffic_limit"),
                         "valid_until": acc.get("valid_until")})
            if args.update:
                acc["proxy_ok"] = ok
                acc["proxy_check"] = detail
                acc["exit_ip"] = detail.split(" ")[0] if ok else acc.get("exit_ip")
                acc["status"] = "active" if ok else "proxy_dead"
                results.save_account(acc)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    alive = sum(1 for r in rows if r["ok"])
    gb = sum((r["gb"] or 0) for r in rows if r["ok"])
    print(f"аккаунтов: {len(rows)} | живых: {alive} | трафик живых: {gb} GB/мес\n")
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        print(f"{mark} {r['email']:<32} {r['username'] or '-':<12} {r['detail'][:56]}")
    print(f"\nИТОГО: {alive}/{len(rows)} живых, {gb} GB/мес")


if __name__ == "__main__":
    main()
