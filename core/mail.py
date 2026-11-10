# -*- coding: utf-8 -*-
"""apimail (drakezakuru.com) — catch-all ящик, чтение через admin API.

GET {MAIL_BASE}/admin/mails?address=<addr>&limit=N&offset=0
  Headers: x-admin-auth: <key>, User-Agent: curl/8.5.0 (иначе 403!)
  Ответ: {"results": [{"id", "raw" (MIME), ...}]}

Ящик создавать не нужно: любой адрес @drakezakuru.com принимается.
Универсально: умеет и verify-ССЫЛКУ (`wait_link`), и 6-значный КОД (`wait_code`).
Хост ссылки берётся из config.BASE_URL — ничего не хардкодим под сервис.
"""
import email as email_module
import email.utils
import os
import random
import re
import string
import time

import requests

from . import config

# 6-значный код (для сервисов, которые шлют код, а не ссылку)
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _code_re():
    """Формат кода из конфига `mail_code_re` (напр. Slack шлёт XXX-XXX:
    r"\\b([A-Z0-9]{3}-[A-Z0-9]{3})\\b"). Пусто/бито — дефолтные 6 цифр."""
    pat = getattr(config, "MAIL_CODE_RE", None)
    if pat:
        try:
            return re.compile(pat)
        except re.error:
            pass
    return CODE_RE


def _link_res():
    """Регулярки под хост сервиса из конфига (BASE_URL)."""
    host = re.escape((getattr(config, "BASE_URL", "") or "")
                     .replace("https://", "").replace("http://", "").rstrip("/"))
    if not host:
        return []
    return [
        re.compile(rf"https://{host}/[^\s\"'<>\\)]*token=[^\s\"'<>\\)]+", re.I),
        re.compile(rf"https://{host}/[^\s\"'<>\\)]*(?:verify|confirm|activate)[^\s\"'<>\\)]*", re.I),
        re.compile(rf"https://{host}/[^\s\"'<>\\)]+", re.I),   # запасной: любая ссылка сервиса
    ]


# Статика из HTML-писем (логотипы, стили, трекеры) — НЕ verify-ссылки.
# Набито на wispbyte (14.10.2026): запасной паттерн «любая ссылка хоста»
# хватал …/assets/wispbyte_blue_nobg.png из шапки письма.
_ASSET_RE = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4|webm|pdf|zip)"
    r"(?:[?#][^\s\"'<>\\)]*)?$", re.I)


def _is_asset(url):
    return bool(_ASSET_RE.search(url))


def _decode_body(raw, sender_hint=None, since_ts=None):
    """MIME → (body, subject) с фильтрами по отправителю и времени.

    sender_hint=None — фильтр выключен (когда имя сервиса заранее неизвестно).
    """
    msg = email_module.message_from_string(raw or "")
    subj = str(email_module.header.make_header(
        email_module.header.decode_header(msg.get("Subject", ""))))
    frm = str(msg.get("From", ""))
    if sender_hint and sender_hint.lower() not in (frm + subj).lower():
        return None, subj
    if since_ts:
        try:
            ts = email_module.utils.mktime_tz(
                email_module.utils.parsedate_tz(msg.get("Date", "")))
            if ts and ts < since_ts - 60:
                return None, subj
        except Exception:
            pass
    body = ""
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)      # декодирует base64/QP
            if payload:
                body += payload.decode("utf-8", "ignore") + "\n"
    return body.replace("&amp;", "&"), subj              # HTML экранирует &


def parse_link_from_raw(raw, sender_hint=None, since_ts=None):
    """MIME → (url, subject); url=None, если ссылки нет.

    Паттерны идут от точного к запасному; ссылки на статику (логотипы и пр.)
    пропускаются всегда — иначе запасной паттерн хватает картинку из шапки.
    """
    body, subj = _decode_body(raw, sender_hint, since_ts)
    if body is None:
        return None, subj
    res = _link_res()
    fallback = None
    for i, rx in enumerate(res):
        m = rx.search(body)
        if not m:
            continue
        url = m.group(0).rstrip(".,;>]")
        if _is_asset(url):
            continue
        if i == len(res) - 1:
            fallback = fallback or url     # запасной — только если точнее нет
            continue
        return url, subj
    return fallback, subj


def parse_code_from_raw(raw, sender_hint=None, since_ts=None):
    """MIME → (code, subject); code=None, если кода нет.

    Берём только 6 цифр подряд — даты (2026-10-07) и годы не считаются кодом.
    """
    body, subj = _decode_body(raw, sender_hint, since_ts)
    if body is None:
        return None, subj
    m = _code_re().search(body)
    return (m.group(1) if m else None), subj


# ── health движков по D1 ───────────────────────────────────────────────────
# Дневной лимит D1 сбрасывается в 00:00 UTC. Движок, который ответил
# "D1_ERROR ... daily row read limit", помечаем «мёртвым» до 00:05 UTC —
# new_address() перестаёт выдавать его домены, а после сброса флаг сам
# истекает: это и есть автовозвращение без ручной правки конфига.
_BAD_UNTIL = {}          # base -> timestamp, до которого не выдаём домены
_PROBED = {"at": 0.0}    # когда последний раз проверяли движки (на процесс)


def _next_utc_reset(extra_min=5):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=extra_min,
                                            second=0, microsecond=0)
    return nxt.timestamp()


def _is_bad(base):
    return _BAD_UNTIL.get(base, 0) > time.time()


def _cf_usage(ep):
    """Дневной расход D1 движка через Cloudflare GraphQL. None — нет доступа.

    Нужен, потому что «пустой» запрос к /admin/mails может вернуть 200 даже
    при выбранном лимите (D1 не читает строки), и ошибку видно только на
    реальном адресе. По расходу же лимит виден сразу.
    """
    cf = ep.get("cf") or {}
    acc = cf.get("account_id")
    tok = cf.get("api_token") or ""
    if not tok and cf.get("token_file"):
        try:
            with open(os.path.expanduser(cf["token_file"]), "r", encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
    if not acc or not tok:
        return None
    q = {"query": "query($acc:String!,$s:Date!,$e:Date!){viewer{accounts("
                  "filter:{accountTag:$acc}){d1AnalyticsAdaptiveGroups("
                  "filter:{date_geq:$s,date_leq:$e},limit:100){sum{rowsRead rowsWritten}}}}}",
         "variables": {"acc": acc,
                       "s": time.strftime("%Y-%m-%d"), "e": time.strftime("%Y-%m-%d")}}
    try:
        r = requests.post("https://api.cloudflare.com/client/v4/graphql",
                          headers={"Authorization": f"Bearer {tok}"},
                          json=q, timeout=12)
        d = r.json()
    except Exception:
        return None
    if d.get("errors"):
        return None
    try:
        groups = d["data"]["viewer"]["accounts"][0]["d1AnalyticsAdaptiveGroups"] or []
    except Exception:
        return None
    read = sum(int(g["sum"].get("rowsRead") or 0) for g in groups)
    return {"read": read, "limit": 5_000_000}


def _mark_bad(base, err=""):
    txt = str(err).lower()
    if ("d1_error" in txt or "daily row read" in txt or "row read limit" in txt
            or "http 500" in txt):
        _BAD_UNTIL[base] = _next_utc_reset()
        return True
    return False


class MailClient:
    def __init__(self, base=None, admin_key=None, domain=None):
        self.base = (base or config.MAIL_BASE).rstrip("/")
        self.key = admin_key or config.MAIL_ADMIN_KEY
        self.domain = domain or config.MAIL_DOMAIN
        self.domains = list(getattr(config, "MAIL_DOMAINS", None) or [self.domain])
        # Мультиаккаунт: несколько catch-all движков (разные Cloudflare-аккаунты
        # с своими D1-квотами). Домен → свой base/admin_key. Пусто — один движок.
        self.endpoints = []
        for ep in (getattr(config, "MAIL_ACCOUNTS", None) or []):
            base = str(ep.get("base") or "").rstrip("/")
            if not base:
                continue
            doms = [str(d).lower() for d in (ep.get("domains") or [])]
            self.endpoints.append({"base": base,
                                   "key": ep.get("admin_key") or self.key,
                                   "domains": doms,
                                   "cf": ep.get("cf") or {}})
        if self.endpoints:
            flat = [d for ep in self.endpoints for d in ep["domains"]]
            if flat:
                self.domains = flat
        else:
            self.endpoints = [{"base": self.base, "key": self.key,
                               "domains": [d.lower() for d in self.domains]}]
        self.dot_domains = list(getattr(config, "MAIL_DOT_DOMAINS", None) or [])
        # gmail plus-алиасы — второй источник в ТОМ ЖЕ клиенте (единый API).
        # Письма алиасов читаются по IMAP напрямую (catch-all worker их не
        # хранит — он знает только свои домены).
        g = getattr(config, "GMAIL", None) or {}
        self.gmail = g if g.get("enabled") else None
        self.gmail_only = bool(getattr(config, "MAIL_ONLY_GMAIL", False))
        self._flip = 0
        self.session = requests.Session()
        self.session.headers.update({
            "x-admin-auth": self.key,
            "User-Agent": config.MAIL_UA,
            "Accept": "application/json",
        })

    def new_address(self, prefix=None):
        """Новый адрес. Домен — из ЖИВЫХ движков (см. _probe_endpoints).

        Если движок упёрся в дневной лимит D1, его домены не выдаются до
        00:05 UTC — это и есть автовозвращение: флаг сам истекает после
        сброса квоты."""
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if self.gmail:
            self._flip += 1
            if self.gmail_only or self._flip % 2 == 0:
                local = self.gmail["user"].split("@")[0]
                tag = (prefix or config.ADDRESS_PREFIX or "mm") + rnd
                return f"{local}+{tag}@gmail.com"
        prefix = prefix or (config.ADDRESS_PREFIX + rnd)
        self._probe_endpoints()
        pairs = [(d, ep) for ep in self.endpoints for d in ep["domains"]]
        healthy = [(d, ep) for d, ep in pairs if not _is_bad(ep["base"])]
        use = healthy or pairs
        dom, _ep = random.choice(use)
        return f"{prefix}@{dom}"

    @staticmethod
    def _is_gmail(address):
        return address.lower().endswith(("@gmail.com", "@googlemail.com"))

    def send_address(self, address):
        """Адрес для ОТПРАВКИ в сервис. К доменам из mail_dot_domains добавляет
        точку в конце (FQDN) — обход блок-листа по точному домену; почтовый
        сервер нормализует FQDN, письмо падает в тот же catch-all."""
        local, _, dom = address.partition("@")
        if not dom or dom.endswith(".") or dom not in self.dot_domains:
            return address
        return f"{local}@{dom}."

    def _probe_endpoints(self, force=False):
        """Один раз на процесс (или force) проверить движки дешёвым запросом.

        Помечает те, что уже упёрлись в дневной лимит D1, чтобы не выдать
        их домены под новый аккаунт (иначе письмо не прочитать)."""
        if not self.endpoints:
            return
        if not force and time.time() - _PROBED["at"] < 300:
            return
        _PROBED["at"] = time.time()
        for ep in self.endpoints:
            u = _cf_usage(ep)
            if u and u["read"] >= u["limit"] * 0.95:
                _BAD_UNTIL[ep["base"]] = _next_utc_reset()
            if _is_bad(ep["base"]) or not ep["domains"]:
                continue
            addr = "probe@" + ep["domains"][0]
            try:
                r = self.session.get(
                    f"{ep['base']}/admin/mails",
                    params={"address": addr, "limit": 1, "offset": 0},
                    headers={"x-admin-auth": ep["key"]}, timeout=8)
                if r.status_code != 200:
                    _mark_bad(ep["base"], (r.text or "") + f" http {r.status_code}")
            except Exception:
                pass

    def _endpoint_for(self, address):
        """Движок, который обслуживает домен адреса (иначе — первый)."""
        dom = address.rpartition("@")[2].lower().rstrip(".")
        for ep in self.endpoints:
            if dom in ep["domains"]:
                return ep
        return self.endpoints[0]

    def list_mails(self, address, limit=10, offset=0):
        ep = self._endpoint_for(address)
        r = self.session.get(f"{ep['base']}/admin/mails",
                             params={"address": address, "limit": limit, "offset": offset},
                             headers={"x-admin-auth": ep["key"]},
                             timeout=25)
        if r.status_code != 200:
            _mark_bad(ep["base"], r.text or f"http {r.status_code}")
            raise Exception(f"mail list {address}: {r.status_code} {r.text[:200]}")
        return r.json().get("results", [])

    def raw_mails(self, address, limit=10, min_id=None, min_uid=None):
        """Сырые письма (MIME-строки) для адреса — единая точка для обоих источников.

        catch-all читается через admin API, gmail-алиас — по IMAP. Реггеру
        не нужно знать, откуда почта: он просит raw и ищет в нём ссылку
        или код своей регуляркой.
        """
        if self._is_gmail(address) and self.gmail:
            out = []
            try:
                m = self._imap()
                m.select("INBOX", readonly=True)
                _, data = m.uid("search", None, "TO", f'"{address}"')
                ids = data[0].split() if data and data[0] else []
                if min_uid:
                    ids = [u for u in ids if int(u) > int(min_uid)]
                for uid in reversed(ids[-limit:]):
                    _, md = m.uid("fetch", uid, "(RFC822)")
                    if md and isinstance(md[0], tuple):
                        out.append(md[0][1].decode("utf-8", "ignore"))
                m.logout()
            except Exception as exc:
                print(f"  [gmail] raw: {str(exc)[:120]}")
            return out
        try:
            mails = self.list_mails(address, limit=limit)
        except Exception:
            return []
        return [m.get("raw") or "" for m in mails
                if not min_id or (m.get("id") or 0) > min_id]

    # ── two-phase: зафиксировать «до» и ждать только новое ───────────────
    def baseline(self, address):
        """Снимок «сколько писем уже было» ДО отправки формы.

        Нужен, когда регистрация и подтверждение разъезжаются по времени:
        аккаунт сохраняется как pending вместе с baseline, а через час
        `verify` берёт только письма, пришедшие ПОСЛЕ регистрации — иначе
        легко подхватить старое письмо от предыдущей попытки.
        """
        snap = {"since_ts": time.time(), "min_id": 0, "min_uid": 0}
        if self._is_gmail(address) and self.gmail:
            try:
                m = self._imap()
                m.select("INBOX", readonly=True)
                _, data = m.uid("search", None, "TO", f'"{address}"')
                ids = data[0].split() if data and data[0] else []
                snap["min_uid"] = int(ids[-1]) if ids else 0
                m.logout()
            except Exception as exc:
                print(f"  [gmail] baseline: {str(exc)[:100]}")
            return snap
        try:
            mails = self.list_mails(address, limit=1)
            snap["min_id"] = max((m.get("id") or 0) for m in mails) if mails else 0
        except Exception as exc:
            print(f"  [mail] baseline: {str(exc)[:120]}")
        return snap

    def _wait(self, address, extractor, sender_hint, timeout, poll, since_ts,
              min_id=None):
        timeout = timeout or config.MAIL_TIMEOUT_SEC
        poll = poll or config.MAIL_POLL_SEC
        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                mails = self.list_mails(address, limit=15)
            except Exception as exc:
                print(f"  [mail] ошибка списка: {str(exc)[:120]}")
                mails = []
            for m in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                mid = m.get("id")
                if mid in seen:
                    continue
                seen.add(mid)
                if min_id and (mid or 0) <= min_id:
                    continue          # письмо пришло ДО попытки — не наше
                val, subj = extractor(m.get("raw", ""), sender_hint=sender_hint,
                                      since_ts=since_ts)
                if val:
                    return val, subj
            print(f"  [mail] письма пока нет ({int(deadline - time.time())}s осталось)...")
            time.sleep(poll)
        return None, None

    def wait_link(self, address, sender_hint=None, timeout=None, poll=None,
                  since_ts=None, min_id=None):
        """Ждёт письмо и возвращает (verify-ссылка, subject) или (None, None)."""
        if self._is_gmail(address) and self.gmail:
            return self._wait_gmail(address, "link", sender_hint, timeout, poll,
                                    since_ts, min_id)
        return self._wait(address, parse_link_from_raw, sender_hint, timeout, poll,
                          since_ts, min_id)

    def wait_code(self, address, sender_hint=None, timeout=None, poll=None,
                  since_ts=None, min_id=None):
        """Ждёт письмо и возвращает (6-значный код, subject) или (None, None)."""
        if self._is_gmail(address) and self.gmail:
            return self._wait_gmail(address, "code", sender_hint, timeout, poll,
                                    since_ts, min_id)
        return self._wait(address, parse_code_from_raw, sender_hint, timeout, poll,
                          since_ts, min_id)

    def wait(self, address, baseline=None, **kw):
        """Ожидание по снимку baseline (см. `baseline()`).

        Универсальная точка для `verify`: сама решает, ссылка нужна или код,
        по наличию `kind` в kw (по умолчанию — ссылка, затем код).
        """
        snap = baseline or {}
        kw.setdefault("since_ts", snap.get("since_ts"))
        if snap.get("min_id"):
            kw.setdefault("min_id", snap["min_id"])
        kind = kw.pop("kind", None)
        if kind == "code":
            return self.wait_code(address, **kw), "code"
        if kind == "link":
            return self.wait_link(address, **kw), "link"
        url, subj = self.wait_link(address, **kw)
        if url:
            return (url, subj), "link"
        code, subj2 = self.wait_code(address, **kw)
        return (code, subj2), "code"

    # ── gmail по IMAP ────────────────────────────────────────────────────
    def _imap(self):
        import imaplib
        import ssl
        g = self.gmail
        ctx = ssl.create_default_context()
        m = imaplib.IMAP4_SSL(g.get("imap_host", g.get("host", "imap.gmail.com")),
                              int(g.get("imap_port", g.get("port", 993))),
                              ssl_context=ctx)
        m.login(g["user"], (g.get("app_password") or "").replace(" ", ""))
        return m

    def _wait_gmail(self, address, kind, sender_hint, timeout, poll, since_ts,
                    min_uid=None):
        timeout = timeout or config.MAIL_TIMEOUT_SEC
        poll = poll or config.MAIL_POLL_SEC
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                m = self._imap()
                m.select("INBOX", readonly=True)
                _, data = m.uid("search", None, "TO", f'"{address}"')
                ids = data[0].split() if data and data[0] else []
                if min_uid:
                    ids = [u for u in ids if int(u) > int(min_uid)]
                for uid in reversed(ids[-3:]):
                    _, md = m.uid("fetch", uid, "(RFC822)")
                    if not md or not isinstance(md[0], tuple):
                        continue
                    raw = md[0][1].decode("utf-8", "ignore")
                    extractor = parse_link_from_raw if kind == "link" else parse_code_from_raw
                    val, subj = extractor(raw, sender_hint=sender_hint, since_ts=since_ts)
                    if val:
                        m.logout()
                        return val, subj
                m.logout()
            except Exception as exc:
                print(f"  [gmail] ошибка: {str(exc)[:100]}")
            time.sleep(poll)
        return None, None
