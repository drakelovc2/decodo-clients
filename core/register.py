# -*- coding: utf-8 -*-
"""decodo-register — ядро: регистрация Decodo (ex-Smartproxy) → 1 GB free DC/мес.

Живая разведка 07.11.2026 (Camoufox + снятые XHR). Кабинет —
https://dashboard.decodo.com, SPA за Cloudflare managed challenge + Akamai +
reCAPTCHA v3 (invisible) + FingerprintJS. Регистрация идёт ТОЛЬКО браузером:
сервер ждёт recaptcha-токен и `ifingerprint`, которые генерит страница.

Цепочка (один браузерный контекст на аккаунт):

  1. GET  /register                      → форма #newEmail/#newPassword/#acceptTerms
  2. POST /api/v1/users/validate-email/  → пре-чек домена (200 = ок)
  3. POST /api/v1/users/                  → {uuid, token}; ответ 200 = аккаунт есть
  4. письмо "Verify your Decodo account"  → ссылка /verify/<hash>/<uuid>/
  5. GET  /verify/...                     → email_verified=true, cookie сессии
  6. POST /subscription-api/v1/api/order/activate-free-dc-proxies  → 204
  7. GET  /subscription-api/v1/subscriptions
         free_dc: 1 GB/мес, 10 US DC IP, valid 30 дней
  8. GET  /subscription-api/v1/subscriptions/<uuid>/proxy-users?traffic_included=1
         → {username, password}
  9. проверка: http://user:pass@dc.decodo.com:10000 → ipinfo.io (US DC)

Контракт шаблона (импортируют panel/server.py):
    register_one(), test_proxy(), list_models(), free_models(), ServiceError.

Секреты (пароли, proxy-креды) пишутся в банк data/accounts.jsonl; в консоль
уходят только маски.
"""
import json
import random
import string
import time
import urllib.parse

from . import config
from .mail import MailClient

SIGNUP = "https://dashboard.decodo.com/register"
PROXY_HOST = "dc.decodo.com"
PROXY_PORT = 10000

# fetch из контекста страницы: cookie + CF/Akamai-заголовки браузера
_JS_FETCH = """
async ({url, method, body}) => {
  const r = await fetch(url, {
    method: method || 'GET', credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  const t = await r.text();
  return {status: r.status, body: t};
}
"""


class ServiceError(Exception):
    pass


def _gen_password(n=14):
    chars = string.ascii_letters + string.digits + "!@#$%"
    pw = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase),
          random.choice(string.digits), random.choice("!@#$%")]
    pw += [random.choice(chars) for _ in range(n - 4)]
    random.shuffle(pw)
    return "".join(pw)


def _proxy_opts(raw):
    """URL прокси → формат Camoufox (server + отдельные username/password)."""
    if not raw:
        return None
    u = urllib.parse.urlsplit(raw)
    if not u.hostname:
        return None
    pw = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        pw["username"] = urllib.parse.unquote(u.username)
        pw["password"] = urllib.parse.unquote(u.password or "")
    return pw


def _wait_cf(page, limit=120):
    d = time.time() + limit
    while time.time() < d:
        t = (page.title() or "").lower()
        if "just a moment" not in t and "security verification" not in t:
            return True
        time.sleep(2)
    return False


def _fetch(page, url, method="GET", body=None):
    r = page.evaluate(_JS_FETCH, {"url": url, "method": method, "body": body})
    return r["status"], r["body"]


def _json_or_none(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── проверка прокси-креда (внешний шаг, без браузера) ───────────────────────
def test_proxy(account, timeout=30):
    """Проверить пару username/password у dc.decodo.com:10000.

    account: dict с proxy_username/proxy_password или строка "user:pass".
    → (ok, detail), detail — exit IP/город или причина.
    """
    if isinstance(account, str):
        user, _, pwd = account.partition(":")
    else:
        user = account.get("proxy_username") or ""
        pwd = account.get("proxy_password") or ""
    if not user or not pwd:
        return False, "нет proxy-кредов"
    px = (f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(pwd)}"
          f"@{PROXY_HOST}:{PROXY_PORT}")
    import requests
    try:
        r = requests.get("https://ipinfo.io/json",
                         proxies={"http": px, "https": px}, timeout=timeout)
    except Exception as exc:
        return False, f"proxy error: {str(exc)[:120]}"
    if not r.ok:
        return False, f"proxy http {r.status_code}"
    try:
        j = r.json()
    except Exception:
        return False, "proxy: не JSON от ipinfo"
    return True, f"{j.get('ip')} ({j.get('city')}, {j.get('country')})"


def test_key(api_key, model=None, proxy=None):
    """Контракт панели: ключей у Decodo нет — проверяем proxy-креды,
    если передан dict, иначе считаем строку парой user:pass."""
    ok, detail = test_proxy(api_key)
    return ok, "proxy", detail


def list_models(api_key=None, proxy=None):
    """У Decodo нет /v1/models — пул это прокси, а не каталог моделей."""
    return []


def free_models(api_key=None, proxy=None):
    return []


# ── основной флоу ──────────────────────────────────────────────────────────
def register_one(email=None, password=None, proxy=None, mail=None,
                 on_step=None, **_kw):
    """Один аккаунт Decodo целиком. Возвращает dict для банка.

    email/password можно передать снаружи; иначе генерируются (почта — через
    MailClient, если он передан). proxy — URL вида http://user:pass@host:port.
    """
    def step(msg):
        if on_step:
            try:
                on_step(msg)
            except Exception:
                pass

    mail = mail or MailClient()
    if not email:
        email = mail.new_address()
    password = password or _gen_password()
    rec = {
        "service": "decodo",
        "email": email,
        "password": password,
        "proxy": proxy or "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "creating",
    }

    baseline = None
    try:
        baseline = mail.baseline(email)
    except Exception:
        pass

    from camoufox.sync_api import Camoufox

    # geoip: таймзона/локаль из exit-IP — иначе CF видит TZ сервера (UTC)
    opts = {"headless": True, "humanize": True, "locale": "en-US", "geoip": True}
    pw = _proxy_opts(proxy)
    if pw:
        opts["proxy"] = pw
        step(f"прокси {pw['server']}")

    with Camoufox(**opts) as browser:
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()

        # 1) форма
        step("открываю /register")
        page.goto(SIGNUP, wait_until="domcontentloaded", timeout=90000)
        if not _wait_cf(page):
            raise ServiceError("Cloudflare challenge не пройден")
        page.wait_for_selector("#newEmail", timeout=90000)
        time.sleep(2)
        page.fill("#newEmail", email)
        page.fill("#newPassword", password)
        # попап «правила пароля» перекрывает чекбокс — кликаем нативно
        page.evaluate("document.getElementById('acceptTerms').click()")
        if not page.evaluate("document.getElementById('acceptTerms').checked"):
            page.evaluate("""() => {
                const cb = document.getElementById('acceptTerms');
                cb.checked = true;
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
        time.sleep(1)
        step("отправляю форму")
        page.evaluate("document.querySelector('button[data-testid=continueRegistration]').click()")

        # 2) ждём аккаунт (SPA сама дергает validate-email + POST /users/)
        user = None
        d = time.time() + 90
        while time.time() < d:
            time.sleep(2)
            st, body = _fetch(page, "/api/v1/user-information")
            if st == 200:
                user = _json_or_none(body)
                if user and user.get("email") == email:
                    break
                user = None
        if not user:
            raise ServiceError("аккаунт не создался (нет /api/v1/user-information)")
        rec.update(uuid=user.get("uuid"), username=user.get("username"))
        step(f"аккаунт создан: {user.get('username')} ({user.get('uuid')})")

        # 3) письмо + верификация
        step("жду письмо о верификации")
        link, _subject = mail.wait_link(email, sender_hint="decodo",
                                        timeout=config.MAIL_TIMEOUT_SEC,
                                        since_ts=(baseline or {}).get("since_ts"))
        if not link:
            raise ServiceError("verify-письмо не пришло")
        link = link.rstrip(").,;")
        step("перехожу по verify-ссылке")
        page.goto(link, wait_until="domcontentloaded", timeout=90000)
        _wait_cf(page)
        time.sleep(8)

        verified = False
        d = time.time() + 60
        while time.time() < d:
            st, body = _fetch(page, "/api/v1/user-information")
            if st == 200:
                j = _json_or_none(body) or {}
                if j.get("email_verified"):
                    verified = True
                    break
            time.sleep(4)
        if not verified:
            raise ServiceError("почта не подтвердилась после verify-ссылки")
        rec["email_verified"] = True
        step("почта подтверждена")

        # 4) активация free DC (1 GB/мес)
        step("активирую free DC proxies")
        st, body = _fetch(page, "/subscription-api/v1/api/order/activate-free-dc-proxies",
                          method="POST", body={})
        if st not in (200, 201, 204):
            raise ServiceError(f"activate-free-dc: http {st} {body[:160]}")
        sub = None
        d = time.time() + 90
        while time.time() < d:
            st, body = _fetch(page, "/subscription-api/v1/subscriptions")
            j = _json_or_none(body) or {}
            if j.get("free_dc"):
                sub = j["free_dc"]
                break
            time.sleep(5)
        if not sub:
            raise ServiceError("free_dc не появился в подписках")
        rec.update(free_dc_uuid=sub.get("uuid"), traffic_limit=sub.get("traffic_limit"),
                   valid_until=sub.get("valid_until"), sub_status=sub.get("status"),
                   payment_processor=sub.get("payment_processor"))
        step(f"free_dc активен: {sub.get('traffic_limit')} GB до {sub.get('valid_until')}")

        # 5) прокси-креды
        step("читаю proxy-креды")
        users = []
        d = time.time() + 60
        while time.time() < d:
            st, body = _fetch(
                page,
                f"/subscription-api/v1/subscriptions/{sub.get('uuid')}/proxy-users?traffic_included=1")
            users = _json_or_none(body) or []
            if users:
                break
            time.sleep(5)
        if not users:
            raise ServiceError("proxy-users пуст")
        pu = users[0]
        rec.update(proxy_username=pu.get("username"),
                   proxy_password=pu.get("password"),
                   proxy_host=PROXY_HOST, proxy_port=PROXY_PORT,
                   proxy_type=pu.get("proxy_type"))
        step(f"proxy-user {pu.get('username')}")

        try:
            rec["cookies"] = [c.get("name") for c in ctx.storage_state().get("cookies", [])]
        except Exception:
            pass

    # 6) внешняя проверка прокси (на VPS порт 10000 может быть закрыт — пропускаем)
    import os as _os
    if _os.environ.get("DECODO_SKIP_PROXY_TEST") == "1":
        ok, detail = True, "проверка пропущена (VPS egress)"
    else:
        step("проверяю прокси через ipinfo")
        ok, detail = test_proxy(rec)
    rec["proxy_ok"] = ok
    rec["proxy_check"] = detail
    if ok:
        rec["exit_ip"] = detail.split(" ")[0]
    rec["status"] = "active" if ok else "proxy_unchecked"
    return rec
