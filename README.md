# decodo-register — авторег Decodo (free DC proxies)

Decodo (ex-Smartproxy) даёт **1 GB datacenter-прокси трафика в месяц бесплатно**
на аккаунт: без карты, только подтверждение почты. Реггер делает аккаунты
пачками и складывает в банк рабочие `username:password` к
`dc.decodo.com:10000`.

## Что получается на аккаунт

| Параметр | Значение |
|---|---|
| Трафик | **1 GB / месяц** (`traffic_limit: 1`) |
| Срок | 30 дней от активации, `payment_processor: free` |
| IP | 10 US datacenter (AS18779 EGIHosting, Santa Clara CA) |
| Протокол | HTTP/HTTPS через `dc.decodo.com:10000` (ротация IP) |
| Креды | `proxy_username` / `proxy_password` в банке |

Проверка креда: `curl -x http://USER:PASS@dc.decodo.com:10000 https://ipinfo.io/json`.
1 GB на аккаунт → пул = сумма аккаунтов (N акк ≈ N GB/мес).

## Запуск

```bash
bash start.sh                 # панель :9941 (+ откроет браузер)
bash start.sh --no-browser    # без окна

# батч из CLI (последовательно, с паузой):
python3 tools/batch.py --count 5 --pause 90
```

Банк: `data/accounts.jsonl` (полные поля), `data/accounts.txt` —
`email|password|user:pass`. Лог батча: `data/logs/batch.log`.

## Флоу (живой recon 07.11.2026)

Всё делает один Camoufox-контекст на аккаунт: кабинет за Cloudflare managed
challenge + Akamai + reCAPTCHA v3 (invisible) + FingerprintJS, поэтому
регистрация только браузером — страница сама генерит recaptcha-токен и
`ifingerprint`.

1. `/register` → `#newEmail`, `#newPassword`, `#acceptTerms` → Continue
2. SPA: `POST /api/v1/users/validate-email/` → `POST /api/v1/users/`
3. письмо «Verify your Decodo account» → ссылка `/verify/<hash>/<uuid>/`
4. открыть ссылку → `email_verified: true` + сессия
5. `POST /subscription-api/v1/api/order/activate-free-dc-proxies` → 204
6. `GET /subscription-api/v1/subscriptions` → `free_dc` (1 GB, active)
7. `GET .../subscriptions/<uuid>/proxy-users?traffic_included=1` → креды
8. проверка через `ipinfo.io` (US DC IP)

Нюансы, набитые на живом тесте:
- попап правил пароля перекрывает чекбокс — кликаем `#acceptTerms` из JS;
- ссылка в письме — quoted-printable: raw MIME надо decode'ить, иначе в токене
  остаётся `=` и `/verify/...` отдаёт 404;
- `/api/order/activate-free-dc-proxies` живёт на префиксе
  `/subscription-api/v1`, без него — 404;
- Cloudflare на `/verify/...` может думать до ~1 минуты — wait-цикл по title.

## Почта (apimail)

Регистрация Decodo требует подтверждения почты — клик по verify-ссылке из письма.
Почтовый слой — **apimail**: собственный catch-all домен + admin API. Письма
принимаются на любой адрес домена (ящик создавать не нужно), читаются через API:

```
GET {mail_base}/admin/mails?address=<addr>&limit=N&offset=0
  headers: x-admin-auth: <mail_admin_key>, User-Agent: curl/8.5.0 (важно, иначе 403)
  ответ:   {"results": [{"id", "raw" (MIME), ...}]}
```

`core/mail.py` умеет и verify-ссылку (`wait_link`, ищет ссылку на хост из
`base_url`), и 6-значный код (`wait_code`). Хост сервиса в реглярках ссылки
собирается из конфига автоматически — под новый сервис ничего не правится.

### Как поднять свой apimail

1. Домен с **Cloudflare Email Routing** → Custom address/Worker на worker
   `temp-mail` (Cloudflare Worker, хранит письма в D1).
2. `mail_base` — адрес worker'а (например `https://mail.example.com`),
   `mail_admin_key` — ключ admin API (заголовок `x-admin-auth`).
3. `mail_domains` — список доменов, на которых worker принимает письма
   (можно несколько: при бане домена добавить новый в CF и сюда).

Альтернатива без своего домена — секция `gmail` в config.json: плюс-алиасы
одного Gmail-ящика (`user+dc123@gmail.com`), чтение по IMAP с app-password.
Подходит как запасной источник, но Gmail лимитирует количество писем.

## Панель

`bash start.sh` → http://127.0.0.1:9941 — запуск прогона, прогресс, список
аккаунтов, перепроверка (для Decodo проверяет прокси-кред через ipinfo).

## Прокси для самой регистрации

По умолчанию — прямое подключение (живой тест: 3 аккаунта подряд с одного IP
прошли). Если включится флуд-защита — передай прокси:

```bash
python3 tools/batch.py --count 3 --proxy 'http://user:pass@host:port'
```

Camoufox получает прокси как `server` + отдельные `username`/`password`
(креды внутри URL он не понимает).

## Структура

```
core/register.py   флоу Decodo (регистрация → верификация → free DC → креды)
core/mail.py       apimail: новый адрес, ожидание verify-ссылки
core/results.py    атомарный банк (JSONL + combo)
panel/             локальная панель :9941
tools/batch.py     последовательный батч
data/              банк, логи (в git не идут)
```

## Ответственное использование

Только свои аккаунты/сервисы или письменное разрешение владельца. Массовая
регистрация может нарушать ToS Decodo.

---

🔥 Telegram: [@antivibedrake](https://t.me/antivibedrake) · [@zakurulife](https://t.me/zakurulife)

Только для личного использования — см. [DISCLAIMER.md](DISCLAIMER.md).
