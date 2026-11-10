#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prune_keys.py — уборка неактивных ключей пула: мёртвые → в архив.

Проверяет КАЖДЫЙ ключ из data/keys.txt живым вызовом и раскладывает:
  * живые   → data/keys.txt (остаются в пуле);
  * мёртвые → data/keys_archive/keys-dead-YYYYMMDD-HHMMSS.txt (с причиной в шапке).

Почему отдельный скрипт, а не только флаг в мосте: мост помечает ключ dead
только когда на него уже пришёл запрос. Ключ с исчерпанным балансом или
отозванный может неделями висеть в пуле и жрать ротацию (в логе было 52
повтора на один такой). Уборка вычищает пул целиком.

    python3 prune_keys.py            # проверить и перенести мёртвые в архив
    python3 prune_keys.py --dry-run  # только показать, ничего не менять
    python3 prune_keys.py --keep-cooling   # не трогать ключи на кулдауне (429)

Код возврата: 0 — пул не пуст, 1 — живых не осталось.
"""
import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_cfg():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = _load_cfg()
# апстрим ОБЯЗАТЕЛЕН в config.json моста (свой у каждого) — дефолта нет,
# чтобы уборка не ушла молча проверять чужой сервис
UPSTREAM = (CFG.get("upstream") or "").rstrip("/")
ARCHIVE_DIR = os.path.join(HERE, "data", "keys_archive")
# проверяем чатом (а не /threads): /threads отдаёт 200 даже при исчерпанном балансе
PROBE_MODEL = CFG.get("prune_probe_model") or CFG.get("default_model") or ""
TIMEOUT = int(CFG.get("prune_timeout_sec", 60))
WORKERS = int(CFG.get("prune_workers", 8))
# эндпоинт баланса задаётся в конфиге: у разных апстримов он свой
BALANCE_PATH = CFG.get("prune_balance_path", "/billing/balance")
# формат запроса чата: openai — {messages:[{role,content}], model} (обычный);
# backboard — {content, model_name, llm_provider} (свой у этого апстрима)
PROBE_STYLE = str(CFG.get("prune_probe_style") or "openai").lower()
# префикс ключей этого пула (напр. "espr_" / "thk_live_"). Если задан, ключи
# другого формата → unknown: это чужой пул, его нельзя трогать.
KEY_PREFIX = str(CFG.get("prune_key_prefix") or CFG.get("key_prefix") or "").strip()


def _resolve_pool():
    """Найти файл пула и способ его чтения/записи.

    Две раскладки (проверено на живых мостах):
      * свой пул:      `data/keys.txt` — по строке на ключ;
      * пул реггера:   `<project_root>/data/accounts.jsonl` — JSONL с полем
        api_key/key, мост читает его напрямую.
    Возвращает (path, kind): kind = "txt" | "jsonl".
    """
    keys_file = CFG.get("keys_file")
    if keys_file:
        p = keys_file if os.path.isabs(keys_file) else os.path.join(HERE, keys_file)
        if os.path.exists(p):
            return p, "txt"
    default_txt = os.path.join(HERE, "data", "keys.txt")
    if os.path.exists(default_txt):
        return default_txt, "txt"
    root = CFG.get("project_root")
    if root:
        p = os.path.normpath(os.path.join(HERE, root, "data", "accounts.jsonl"))
        if os.path.exists(p):
            return p, "jsonl"
    return default_txt, "txt"


POOL_PATH, POOL_KIND = _resolve_pool()
STATE_FILE = os.path.join(HERE, CFG.get("state_file", "data/keys_state.json"))


def load_keys():
    """Ключи из пула: txt — построчно, jsonl — поле api_key/key."""
    if POOL_KIND == "jsonl":
        keys = []
        with open(POOL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = r.get("api_key") or r.get("key")
                if k:
                    keys.append(k)
        return keys
    with open(POOL_PATH, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def write_keys(keep):
    """Записать живой пул обратно. txt — строки; jsonl — фильтр по api_key."""
    if POOL_KIND == "jsonl":
        lines, kept = [], set(keep)
        with open(POOL_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except ValueError:
                    continue
                if (r.get("api_key") or r.get("key")) in kept:
                    lines.append(s)
        with open(POOL_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")


def check(key):
    """(key, status, reason): status = alive | dead | cooling | unknown.

    ВАЖНО: эндпоинты и формат запроса у каждого апстрима свои. Если апстрим
    отвечает «неизвестный ключ» на СЛУЖЕБНОМ эндпоинте (баланс), это может быть
    неверный путь в конфиге, а не бан ключа. Поэтому:
      * баланс — только подсказка, не приговор;
      * решение принимает ЧАТ-запрос;
      * если чат-эндпоинт отвечает 404/405 (не тот путь) — статус unknown,
        ключ НЕ выбрасываем (иначе вычистим весь пул из-за ошибки в конфиге).
    """
    H = {"X-API-Key": key, "Content-Type": "application/json",
         "Authorization": f"Bearer {key}"}
    # чужой формат ключа = чужой пул: не трогаем (иначе вычистим не тот мост)
    if KEY_PREFIX and not key.startswith(KEY_PREFIX):
        return key, "unknown", f"чужой формат ключа (ждём {KEY_PREFIX}…)"
    hint = ""
    try:
        b = requests.get(f"{UPSTREAM}{BALANCE_PATH}", headers=H, timeout=TIMEOUT)
        if b.status_code == 200:
            j = b.json()
            paid = j.get("paid_credit_usd")
            if paid is not None and float(paid) < 0:
                hint = f"баланс в минусе (paid={paid})"
    except requests.RequestException:
        pass
    # чат — единственная надёжная проверка
    if PROBE_STYLE == "openai":
        payload = {"model": PROBE_MODEL, "max_tokens": 5,
                   "messages": [{"role": "user", "content": "ping"}]}
    else:
        payload = {"content": "ping", "model_name": PROBE_MODEL,
                   "llm_provider": CFG.get("llm_provider", "anthropic"), "max_tokens": 5}
    chat_path = CFG.get("prune_chat_path", "/threads/messages")
    try:
        r = requests.post(f"{UPSTREAM}{chat_path}", headers=H, json=payload, timeout=TIMEOUT)
    except requests.RequestException as e:
        return key, "cooling", f"сеть: {type(e).__name__}"
    if r.status_code == 200:
        t = r.text.lower()
        if "balance is used up" in t or "paused llm chat" in t:
            return key, "dead", "баланс исчерпан"
        if "credit is reserved" in t:
            return key, "dead", "только free-кредит (чат не оплачен)"
        if hint:
            return key, "dead", hint
        return key, "alive", ""
    if r.status_code in (401, 403):
        low = r.text.lower()
        # «Invalid API Key FORMAT» — ключ не подходит этому апстриму вовсе
        # (чужой пул / чужой путь). Это не бан: не трогаем.
        if "format" in low or "invalid api key format" in low:
            return key, "unknown", f"чужой формат ключа для {UPSTREAM}: {r.text[:60]}"
        return key, "dead", f"{r.status_code}: {r.text[:80]}"
    if r.status_code in (404, 405):
        # путь чата не тот — это ошибка конфига, а не ключа
        return key, "unknown", f"путь чата {chat_path} → {r.status_code} (проверь prune_chat_path)"
    if r.status_code == 429:
        return key, "cooling", "429 rate limit"
    return key, "cooling", f"HTTP {r.status_code}: {r.text[:80]}"


def archive_dead(dead):
    """Перенести мёртвые ключи в data/keys_archive/ с причиной в шапке."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"keys-dead-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# мёртвые ключи, отбраковано {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# всего: {len(dead)}\n")
        for key, reason in dead:
            f.write(f"# {reason}\n{key}\n")
    return path


def sync_state(alive):
    """Убрать из keys_state.json записи о ключах, которых больше нет в пуле."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return
    keep = set(alive)
    for field in ("keys", "assistants"):
        if isinstance(st.get(field), dict):
            st[field] = {k: v for k, v in st[field].items() if k in keep}
    st["threads"] = {k: v for k, v in (st.get("threads") or {}).items()
                     if v.get("key") in keep}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-cooling", action="store_true",
                    help="не трогать ключи с сетевыми/429-ошибками (оставить в пуле)")
    args = ap.parse_args()

    if not UPSTREAM:
        print("ОШИБКА: в config.json моста не задан `upstream` — "
              "некуда проверять ключи. Пул не тронут.")
        return 2

    keys = load_keys()
    print(f"ключей в пуле: {len(keys)}")
    if not keys:
        print("пул пуст")
        return 1

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        res = list(ex.map(check, keys))

    alive = [k for k, s, _ in res if s == "alive"]
    dead = [(k, r) for k, s, r in res if s == "dead"]
    cooling = [(k, r) for k, s, r in res if s == "cooling"]
    unknown = [(k, r) for k, s, r in res if s == "unknown"]

    print(f"  живых:    {len(alive)}")
    print(f"  мёртвых:  {len(dead)}")
    print(f"  спорных:  {len(cooling)} (сеть/429)")
    if unknown:
        print(f"  НЕИЗВЕСТНО: {len(unknown)} — конфиг неверен, ключи НЕ трогаем")
    for k, r in dead[:15]:
        print(f"    ✗ {k[:20]}… — {r}")
    for k, r in cooling[:8]:
        print(f"    ? {k[:20]}… — {r}")
    for k, r in unknown[:3]:
        print(f"    ! {k[:20]}… — {r}")

    # защита от выжигания пула из-за неверного конфига: если ХОТЬ ОДИН ключ дал
    # «путь не тот» — значит апстрим/путь настроены неверно, пул не переписываем
    if unknown:
        print("\nСТОП: часть ключей не проверить (неверный путь/апстрим). "
              "Проверь prune_chat_path / upstream в config.json. Пул не изменён.")
        return 1

    if args.dry_run:
        print("\n(dry-run: ничего не изменено)")
        return 0 if alive else 1

    if dead:
        path = archive_dead(dead)
        print(f"\nархив мёртвых: {os.path.relpath(path, HERE)} ({len(dead)} шт)")

    # в пуле оставляем живых; спорные — тоже, если просили
    keep = alive + ([k for k, _ in cooling] if args.keep_cooling else [])
    if not keep:
        print("живых ключей не осталось — пул НЕ перезаписан, чтобы не потерять файл")
        return 1

    if os.path.exists(POOL_PATH):
        shutil.copy(POOL_PATH, POOL_PATH + f".bak-prune-{time.strftime('%H%M%S')}")
    write_keys(keep)
    sync_state(keep)
    print(f"пул перезаписан: {len(keep)} ключей "
          f"(живых {len(alive)}" + (f" + спорных {len(cooling)}" if args.keep_cooling else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
