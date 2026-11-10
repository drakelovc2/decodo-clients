# -*- coding: utf-8 -*-
"""reCAPTCHA v2: свой браузер + свой vision-стек($0), 2captcha как fallback.

Типовая задача: сервис ставит reCAPTCHA v2 на форму (поле вида
`captchaToken`). Токен привязан к домену сайта, поэтому минтим его НА САМОЙ
странице: рендерим виджет в отдельный div, жмём чекбокс, а если выпал
картинка-челлендж («Select all images with crosswalks») — решаем сетку и
кликаем по ячейкам.

Два солвера сетки, порядок задаётся `grid_solver`:

    classifier  ONNX YOLO11x-cls на 57k тайлов, 14 классов — ~0.2 с, дёшево
    vlm         OpenAI-совместимая VLM (`vlm_api_base/_key/_model`) — ~15 с,
                «понимает» любую инструкцию, нужна для 4×4 и «none left»
    auto        классификатор → VLM, если классификатор не нашёл ничего
    both        объединение ответов (медленнее, полнее)

Аудио-челлендж — в `vision.solve_audio_captcha` (Whisper-совместимый STT).
Оба своих пути стоят $0; verify идёт fetch'ем из той же страницы, так что
origin/TLS-контекст совпадает по построению.

Готовый стек под это (без GPU, 4 vCPU): `провайдеры/sandboxes-vm/instabox-vision.md`
— ONNX-классификатор, LFM3-VL-2B-recaptcha на llama.cpp и faster-whisper.
"""
import base64
import json
import os
import re
import time

import requests

from . import config

TWOCAPTCHA_CREATE = "https://api.2captcha.com/createTask"
TWOCAPTCHA_RESULT = "https://api.2captcha.com/getTaskResult"

# recaptcha.net — зеркало для регионов, где google.com недоступен.
# Важно: через наши прокси www.google.com отдаёт ProxyError, а
# www.recaptcha.net — 200; токен тот же, sitekey тот же.
_LOAD_JS = """() => new Promise((resolve) => {
  if (window.grecaptcha && window.grecaptcha.render) return resolve(true);
  window.__mcap_onload = () => resolve(true);
  const srcs = [
    'https://www.recaptcha.net/recaptcha/api.js?render=explicit&onload=__mcap_onload',
    'https://www.google.com/recaptcha/api.js?render=explicit&onload=__mcap_onload'
  ];
  let i = 0;
  const next = () => {
    if (i >= srcs.length) return resolve(false);
    const s = document.createElement('script');
    s.src = srcs[i++];
    s.onerror = () => { s.remove(); next(); };
    document.head.appendChild(s);
  };
  next();
  setTimeout(() => resolve(!!(window.grecaptcha && window.grecaptcha.render)), 15000);
})"""

_RENDER_JS = """(sitekey) => {
  try {
    let box = document.getElementById('__mcap_box');
    if (!box) {
      box = document.createElement('div');
      box.id = '__mcap_box';
      box.style.cssText = 'position:fixed;left:24px;top:24px;z-index:2147483647;background:#fff;padding:8px;border-radius:8px;';
      document.body.appendChild(box);
    }
    if (window.__mcap_widget === undefined || window.__mcap_widget === null) {
      window.__mcap_done = 0;
      window.__mcap_widget = grecaptcha.render(box, {
        sitekey: sitekey,
        callback: () => { window.__mcap_done = 1; },
        'expired-callback': () => { window.__mcap_done = 0; }
      });
    }
    return String(window.__mcap_widget);
  } catch (e) { return 'ERR:' + e; }
}"""

_TOKEN_JS = """() => {
  try {
    const w = window.__mcap_widget;
    const v = (w === undefined || w === null) ? '' : grecaptcha.getResponse(w);
    return v || '';
  } catch (e) { return ''; }
}"""

# bframe челленджа: сетка ячеек + инструкция + VERIFY
_BFRAME = 'iframe[src*="api2/bframe"]'
_TABLE = 'table[class*="rc-imageselect-table"]'
_INSTR = '.rc-imageselect-desc-wrapper'
_TILES = 'table[class*="rc-imageselect-table"] td.rc-imageselect-tile'

GRID_PROMPT = (
    "This is a reCAPTCHA image grid ({rows}x{cols}). Cells are numbered "
    "left-to-right, top-to-bottom starting at 0. Instruction: {question}\n"
    "Look at each cell and decide if it contains the requested object. "
    "Answer ONLY JSON: {{\"cells\": [<indices>], \"confidence\": <0-1>}}"
)


def _log(msg):
    print(f"  [captcha] {msg}", flush=True)


# ── VLM ────────────────────────────────────────────────────────────────────
def vlm_cfg():
    base = (os.environ.get("VLM_API_BASE") or config.get("vlm_api_base") or "").rstrip("/")
    key = os.environ.get("VLM_API_KEY") or config.get("vlm_api_key") or ""
    model = os.environ.get("VLM_MODEL") or config.get("vlm_model") or ""
    return base, key, model


def ask_vlm(image_path, prompt, timeout=None):
    """Картинка + текст → ответ VLM (текст). Ошибка → пустая строка.

    Обязательно стримом: CPU-VLM думает 10–20 с, а приватный порт Instabox
    обрывает молчащий запрос на ~20-й секунде (502 Bad Gateway). SSE отдаёт
    заголовки сразу, и соединение живёт сколько нужно.
    """
    base, key, model = vlm_cfg()
    if not (base and key and model):
        _log("VLM не настроен (vlm_api_base/vlm_api_key/vlm_model)")
        return ""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        _log(f"VLM: не прочитать скриншот: {exc}")
        return ""
    body = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0,
        "stream": True,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }
    try:
        r = requests.post(base + "/chat/completions", json=body, timeout=timeout or 180,
                          stream=True,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            _log(f"VLM HTTP {r.status_code}: {r.text[:120]}")
            return ""
        out = []
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                d = json.loads(chunk)
            except ValueError:
                continue
            for ch in d.get("choices") or []:
                delta = ch.get("delta") or ch.get("message") or {}
                for field in ("content", "reasoning_content"):
                    if delta.get(field):
                        out.append(delta[field])
        return "".join(out).strip()
    except Exception as exc:
        _log(f"VLM: {type(exc).__name__}: {str(exc)[:100]}")
        return ""


# ── классификатор тайлов (быстрый путь) ────────────────────────────────────
def grid_cfg():
    base = (os.environ.get("GRID_API_BASE") or config.get("grid_api_base") or "").rstrip("/")
    key = os.environ.get("GRID_API_KEY") or config.get("grid_api_key") or ""
    return base, key


def classify_tiles(paths, question, timeout=25):
    """Оригиналы тайлов → (cells, confidence, label). Без классификатора — None.

    В отличие от VLM, эта штука не «рассуждает», а классифицирует каждый
    тайл по 14 классам reCAPTCHA: ~0.2 с на сетку вместо ~15 с. Зато видит
    только те объекты, на которых обучена (светофор, зебра, автобус, …),
    поэтому в solve_grid_challenge она идёт первой, а VLM добирает, если
    классификатор не нашёл ничего.
    """
    base, key = grid_cfg()
    if not (base and key):
        return None, 0.0, ""
    tiles = []
    for path in paths:
        if not path:
            return None, 0.0, ""
        try:
            with open(path, "rb") as f:
                tiles.append(base64.b64encode(f.read()).decode())
        except OSError:
            return None, 0.0, ""
    try:
        r = requests.post(base + "/v1/captcha/tiles",
                          json={"question": question, "tiles": tiles},
                          timeout=timeout,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            _log(f"классификатор HTTP {r.status_code}: {r.text[:120]}")
            return None, 0.0, ""
        d = r.json()
        return (d.get("cells") or [], float(d.get("confidence") or 0.0),
                d.get("label") or "")
    except Exception as exc:
        _log(f"классификатор: {type(exc).__name__}: {str(exc)[:100]}")
        return None, 0.0, ""


def parse_cells(text, limit):
    """Ответ VLM → индексы ячеек. Терпимо к ```json, лишнему тексту.

    Модели отвечают по-разному: {"cells": [...]}, {"answer": [...]} (так
    делает LFM3-VL), просто списком. Любой из вариантов — валиден.
    """
    if not text:
        return [], 0.0
    cells, conf = [], 0.0
    for m in re.finditer(r"\{[\s\S]*?\}", text):
        try:
            j = json.loads(m.group(0))
        except (ValueError, TypeError):
            continue
        if not isinstance(j, dict):
            continue
        got = j.get("cells") or j.get("answer") or j.get("indices") or j.get("tiles")
        if got:
            cells = got
            try:
                conf = float(j.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            break
    if not cells:
        nums = re.findall(r"\b(\d{1,2})\b", text)
        cells = [int(n) for n in nums]
    if isinstance(cells, dict):
        cells = [k for k, v in cells.items() if v]
    out = sorted({int(c) for c in cells
                  if isinstance(c, (int, str)) and re.fullmatch(r"\d+", str(c).strip())})
    return [c for c in out if 0 <= c < limit], conf


def _label_grid(img_path, rows, cols, out_path):
    """Нарисовать номера ячеек на скриншоте сетки (VLM так точнее)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return img_path
    try:
        img = Image.open(img_path).convert("RGB")
        w, h = img.size
        d = ImageDraw.Draw(img, "RGBA")
        cw, ch = w / cols, h / rows
        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                x, y = int(c * cw), int(r * ch)
                d.rectangle([x + 1, y + 1, x + cw - 2, y + ch - 2],
                            outline=(0, 200, 0, 255), width=2)
                tx, ty = x + 4, y + 3
                d.rectangle([tx - 2, ty - 1, tx + 20, ty + 13], fill=(0, 0, 0, 190))
                d.text((tx, ty), str(idx), fill=(255, 255, 0, 255))
        img.save(out_path)
        return out_path
    except Exception:
        return img_path


def _wait_tiles_loaded(page, timeout=15):
    """Дождаться полной загрузки картинок сетки.

    reCAPTCHA отдаёт прогрессивный JPEG: если снять скриншот сразу, VLM видит
    полузагруженную «дизерингую» картинку и гадает. Ждём img.complete и
    naturalWidth>0 у всех ячеек.
    """
    bf = page.frame_locator(_BFRAME)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ready = bf.locator(_TILES).evaluate_all(
                "els => els.every(td => { const i = td.querySelector('img');"
                " return i && i.complete && i.naturalWidth > 0; })")
        except Exception:
            ready = False
        if ready:
            time.sleep(0.6)
            return True
        time.sleep(0.4)
    return False


def _challenge_error(page):
    """Текст ошибки под сеткой («Please try again» и т.п.), если есть."""
    try:
        bf = page.frame_locator(_BFRAME)
        el = bf.locator('.rc-imageselect-incorrect-response')
        if el.count() and el.first.is_visible():
            return el.first.inner_text(timeout=1500).strip()
    except Exception:
        pass
    return ""


def _selected_count(page):
    """Сколько ячеек реально отмечено (клик зарегистрировался)."""
    try:
        return page.frame_locator(_BFRAME).locator(
            'td.rc-imageselect-tile.rc-imageselect-tileselected').count()
    except Exception:
        return -1


def _tile_srcs(page):
    """URL'ы оригинальных payload-картинок ячеек (300x300)."""
    try:
        return page.frame_locator(_BFRAME).locator(_TILES).evaluate_all(
            "els => els.map(td => { const i = td.querySelector('img'); return i ? i.src : ''; }).filter(Boolean)")
    except Exception:
        return []


def _fetch_tiles(srcs, proxy, out_dir, tag):
    """Скачать оригиналы ячеек через тот же прокси → список путей."""
    paths = []
    for i, src in enumerate(srcs):
        path = os.path.join(out_dir, f"{tag}-tile{i}.jpg")
        try:
            r = requests.get(src, timeout=30,
                             proxies={"http": proxy, "https": proxy} if proxy else None,
                             headers={"User-Agent": config.UA, "Referer": "https://www.recaptcha.net/"})
            if r.status_code == 200 and r.content:
                with open(path, "wb") as f:
                    f.write(r.content)
                paths.append(path)
                continue
        except Exception:
            pass
        paths.append("")
    return paths


def _compose_montage(paths, rows, cols, out_path, size=300):
    """Склеить оригиналы в одну сетку с номерами (VLM видит качество 1:1).

    Скриншот bframe даёт 378px из 300px исходника (апскейл + сглаживание),
    из-за чего картинки «шумят». Оригиналы payload — 300×300, их и показываем.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    canvas = Image.new("RGB", (cols * size, rows * size), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    for idx, path in enumerate(paths[:rows * cols]):
        r, c = divmod(idx, cols)
        if path:
            try:
                im = Image.open(path).convert("RGB").resize((size, size), Image.LANCZOS)
            except Exception:
                im = Image.new("RGB", (size, size), (220, 220, 220))
        else:
            im = Image.new("RGB", (size, size), (220, 220, 220))
        canvas.paste(im, (c * size, r * size))
        d.rectangle([c * size + 1, r * size + 1,
                     (c + 1) * size - 2, (r + 1) * size - 2],
                    outline=(0, 200, 0), width=3)
        d.rectangle([c * size + 4, r * size + 4,
                     c * size + 34, r * size + 28], fill=(0, 0, 0))
        d.text((c * size + 10, r * size + 8), str(idx), fill=(255, 255, 0))
    canvas.save(out_path, quality=90)
    return out_path


def _grid_shape(page):
    """Число строк/столбцов видимой сетки (3x3 или 4x4) из bframe."""
    return page.frame_locator(_BFRAME).locator(_TABLE).evaluate(
        """(t) => {
          const rows = t.querySelectorAll('tr').length || 3;
          const first = t.querySelector('tr');
          const cols = first ? first.querySelectorAll('td').length : 3;
          return [rows, cols];
        }""")


def solve_grid_challenge(page, rounds=6, proxy=None):
    """Решить видимый челлендж-сетку. True — VERIFY нажат.

    4x4-«динамическая» сетка заменяет нажатые ячейки новыми — поэтому
    цикл: VLM → клики → подождать замену → снова, пока VLM не скажет
    «больше нет» → VERIFY. Для 3x3 хватает одного прохода + VERIFY.
    """
    bf = page.frame_locator(_BFRAME)
    try:
        bf.locator(_TABLE).wait_for(state="visible", timeout=15000)
    except Exception as exc:
        _log(f"сетка не появилась: {str(exc)[:80]}")
        return False

    out_dir = os.path.join(config.DATA_DIR, "captcha")
    os.makedirs(out_dir, exist_ok=True)
    for rnd in range(1, rounds + 1):
        try:
            question = bf.locator(_INSTR).inner_text(timeout=5000).strip()
        except Exception:
            question = "Select all images matching the instruction."
        question = re.sub(r"\s+", " ", question)
        try:
            rows, cols = _grid_shape(page)
        except Exception:
            rows, cols = 3, 3
        total = rows * cols
        _wait_tiles_loaded(page)
        tag = f"grid-{rows}x{cols}-{rnd}"
        shot = os.path.join(out_dir, tag + ".png")
        try:
            bf.locator(_TABLE).screenshot(path=shot, timeout=15000)
        except Exception as exc:
            _log(f"скриншот сетки: {str(exc)[:80]}")
        srcs = _tile_srcs(page)
        paths = _fetch_tiles(srcs, proxy, out_dir, tag)

        # grid_solver: classifier (0.2 с, только 14 обученных классов) |
        # vlm (15 с, «понимает» любую инструкцию) | both (объединение, самое
        # полное но самое медленное) | auto = classifier, а если он не нашёл
        # ничего — vlm.
        mode = (config.get("grid_solver") or "auto").lower()
        cells, conf, src = [], 0.0, ""
        if mode in ("auto", "classifier", "both") and paths and all(paths):
            got, cconf, label = classify_tiles(paths, question)
            if got:
                cells, conf, src = list(got), cconf, f"cls:{label}"

        asked_vlm = False
        if mode in ("vlm", "both") or (mode == "auto" and not cells):
            labeled = _compose_montage(paths, rows, cols,
                                       os.path.join(out_dir, tag + "-n.png"))
            if not labeled:
                labeled = _label_grid(shot, rows, cols, os.path.join(out_dir, tag + "-n2.png"))
            prompt = GRID_PROMPT.format(rows=rows, cols=cols, question=question)
            text = ask_vlm(labeled, prompt)
            asked_vlm = True
            vcells, vconf = parse_cells(text, total)
            if mode == "both" and cells:
                cells = sorted(set(cells) | set(vcells))
                conf = max(conf, vconf)
                src += "+vlm"
            elif vcells:
                cells, conf, src = vcells, vconf, "vlm"
            elif not cells:
                src = "vlm"
        _log(f"сетка {rows}x{cols}, «{question[:60]}» → {cells} "
             f"(conf {conf:.2f}, {src or 'пусто'}{' , vlm' if asked_vlm and mode == 'both' else ''})")
        if not cells:
            break
        tds = bf.locator(_TILES)
        for idx in cells:
            try:
                tds.nth(idx).click(timeout=8000)
                time.sleep(0.25)
            except Exception as exc:
                _log(f"клик {idx}: {str(exc)[:60]}")
        sel = _selected_count(page)
        if sel >= 0:
            _log(f"отмечено ячеек: {sel} (кликали {len(cells)})")
        # Динамическая сетка (в инструкции «Click verify once there are none
        # left») заменяет нажатые ячейки новыми — надо продолжать, пока VLM не
        # скажет «больше нет». Классическая 3x3 без замены — сразу VERIFY.
        dynamic = "none left" in question.lower() or total > 9
        if not dynamic:
            break
        time.sleep(2.5)               # дождаться подмены ячеек
    try:
        bf.locator("#recaptcha-verify-button").click(timeout=8000)
        time.sleep(1.5)
        err = _challenge_error(page)
        _log("VERIFY нажат" + (f", ответ: {err[:80]}" if err else ""))
        return True
    except Exception as exc:
        _log(f"VERIFY: {str(exc)[:80]}")
        return False


def solve_in_page(page, sitekey, timeout=240, challenge_rounds=8, proxy=None):
    """Минт reCAPTCHA v2 в контексте страницы. Токен или None.

    page — playwright/camoufox Page уже на домене сервиса (иначе sitekey не
    примется: Google сверяет домен). Каждый челлендж решается VLM-солвером;
    при провале рекапча даёт новый челлендж — пробуем `challenge_rounds` раз.
    """
    if not sitekey:
        return None
    try:
        page.evaluate(_LOAD_JS)
    except Exception as exc:
        _log(f"api.js не поднялся: {str(exc)[:100]}")
        return None
    if str(page.evaluate(_RENDER_JS, sitekey)).startswith("ERR"):
        # повторная отрисовка того же виджета — ок, ошибку мог дать первый вызов
        pass
    try:
        fl = page.frame_locator('iframe[src*="api2/anchor"]')
        anchor = fl.locator("#recaptcha-anchor")
        anchor.wait_for(state="visible", timeout=20000)
        time.sleep(0.8)
        anchor.click(timeout=8000)
    except Exception as exc:
        _log(f"клик по чекбоксу: {str(exc)[:80]}")

    deadline = time.time() + timeout
    solved_rounds = 0
    while time.time() < deadline:
        try:
            tok = page.evaluate(_TOKEN_JS)
        except Exception:
            tok = ""
        if tok:
            _log(f"токен получен ({len(tok)} симв.)")
            return tok
        # есть ли челлендж, который надо решать
        try:
            challenge = page.frame_locator(_BFRAME).locator(_TABLE).count() > 0
        except Exception:
            challenge = False
        if challenge and solved_rounds < challenge_rounds:
            solved_rounds += 1
            _log(f"челлендж {solved_rounds}/{challenge_rounds}")
            solve_grid_challenge(page, proxy=proxy)
            time.sleep(3)
            continue
        time.sleep(2)
    return None


def solve_2captcha(sitekey, pageurl=None, timeout=180):
    """Fallback: RecaptchaV2TaskProxyless (так работал чужой фарм)."""
    key = (config.get("twocaptcha_key") or "").strip()
    if not key or not sitekey:
        return None
    siteurl = pageurl or (config.get("base_url") or "") + "/"
    try:
        r = requests.post(TWOCAPTCHA_CREATE, json={
            "clientKey": key,
            "task": {"type": "RecaptchaV2TaskProxyless",
                     "websiteURL": siteurl, "websiteKey": sitekey},
        }, timeout=30).json()
    except Exception as exc:
        _log(f"2captcha create: {str(exc)[:100]}")
        return None
    if r.get("errorId"):
        _log(f"2captcha create err: {r.get('errorCode')} {r.get('errorDescription')}")
        return None
    tid = r.get("taskId")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        try:
            g = requests.post(TWOCAPTCHA_RESULT, json={"clientKey": key, "taskId": tid},
                              timeout=25).json()
        except Exception:
            continue
        if g.get("status") == "ready":
            tok = (g.get("solution") or {}).get("gRecaptchaResponse")
            _log(f"2captcha: токен получен ({len(tok or '')} симв.)")
            return tok
        if g.get("errorId"):
            _log(f"2captcha poll err: {g.get('errorCode')}")
            return None
    return None


def solve(page, sitekey, pageurl=None, proxy=None):
    """auto: браузер+VLM ($0) → 2captcha. Возвращает токен или None."""
    mode = (config.get("captcha_solver") or "auto").lower()
    if mode in ("vlm", "browser", "auto"):
        tok = solve_in_page(page, sitekey, proxy=proxy)
        if tok:
            return tok
        if mode in ("vlm", "browser"):
            return None
    return solve_2captcha(sitekey, pageurl=pageurl)


def info():
    """Для панели: чем решаем и что настроено."""
    mode = (config.get("captcha_solver") or "auto").lower()
    base, key, model = vlm_cfg()
    gbase, gkey = grid_cfg()
    has_2c = bool((config.get("twocaptcha_key") or "").strip())
    label = {"vlm": "свой браузер + VLM ($0)",
             "browser": "свой браузер + VLM ($0)",
             "2captcha": "2captcha",
             "auto": "свой браузер + VLM ($0) → 2captcha"}.get(mode, mode)
    return {"type": "reCAPTCHA v2", "solver": label, "vlm": model, "vlm_ready": bool(base and key and model),
            "grid_solver": (config.get("grid_solver") or "auto"),
            "grid_classifier": bool(gbase and gkey),
            "sitekey": config.get("recaptcha_sitekey") or "", "twocaptcha": has_2c}