"""HTTP-сервер: вебхук WhatsApp (только номер 7000) и дашборд обращений (только чтение).

Запуск: python app.py   (настройки — в .env рядом)
По умолчанию DEMO_MODE=1: база в памяти заполняется случайными данными (demo_data.py),
вход без пароля, Meta и amoCRM не вызываются. Боевой режим — DEMO_MODE=0 в .env.
Без внешних библиотек: только стандартный Python 3.10+.
"""
import hashlib
import hmac
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from amocrm import AmoSync
from attribution import extract_messages, record_message
from config import BASE_DIR, load_config
from db import Database
from logutil import log
from meta_ads import AdNameFetcher
from report import build_summary, build_report, report_csv

WEB_DIR = BASE_DIR / "web"
STATIC_FILES = {"style.css": "text/css; charset=utf-8",
                "dashboard.js": "application/javascript; charset=utf-8"}
MAX_BODY = 2 * 1024 * 1024
COOKIE_NAME = "d7000_session"
MESSAGES_KEEP_DAYS = 14
AMO_CONTACTS_KEEP_DAYS = 30
QUIET_PATHS = ("/webhook", "/amo/webhook", "/health")  # их тысячи в день — в журнал только ошибки
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def check_signature(app_secret, body, header):
    """X-Hub-Signature-256 = sha256=HMAC(App Secret, сырое тело запроса)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def make_session(secret, ttl_days, now=None):
    exp = int((now if now is not None else time.time()) + ttl_days * 86400)
    sig = hmac.new(secret, f"v1.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"v1.{exp}.{sig}"


def session_valid(secret, value, now=None):
    parts = (value or "").split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    expected = hmac.new(secret, f"v1.{parts[1]}".encode(), hashlib.sha256).hexdigest()
    return (hmac.compare_digest(expected, parts[2])
            and int(parts[1]) > (now if now is not None else time.time()))


class App:
    def __init__(self, cfg, db=None, fetcher=None, amo=None):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.fetcher = fetcher or AdNameFetcher(self.db, cfg)
        self.amo = amo or AmoSync(self.db, cfg)
        self._fails = {}
        self._fails_lock = threading.Lock()

    # --- вебхук ---

    def verify_webhook(self, params):
        token = params.get("hub.verify_token", "")
        if (params.get("hub.mode") == "subscribe" and self.cfg.verify_token
                and hmac.compare_digest(token, self.cfg.verify_token)):
            log("[webhook] проверка Meta пройдена")
            return 200, params.get("hub.challenge", "")
        if params.get("hub.mode"):  # это попытка подключения, а не случайный заход браузером
            hint = f"«{token[:4]}…», {len(token)} симв." if token else "нет"
            log(f"[webhook] проверка Meta НЕ пройдена: кодовое слово {hint}, "
                f"ожидается «{self.cfg.verify_token[:4]}…», {len(self.cfg.verify_token)} симв.")
        return 403, "forbidden"

    def receive_webhook(self, body, signature):
        if self.cfg.app_secret and not check_signature(self.cfg.app_secret, body, signature):
            return 403, "bad signature"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, "bad json"
        counts = {}
        for m in extract_messages(payload, self.cfg.phone_number_id):
            result = record_message(self.db, m, self.cfg.inquiry_gap_days)
            counts[result] = counts.get(result, 0) + 1
        if counts.get("new_ad") or counts.get("new_organic"):  # в журнал — только новые обращения
            log(f"[webhook] {counts}")
        if counts.get("new_ad"):
            self.fetcher.wake()
        return 200, "ok"

    def receive_amo_webhook(self, params, body):
        """Вебхук amoCRM «сделка добавлена». Подписи у amoCRM нет — защищаемся секретом в адресе."""
        key = params.get("key", "")
        if not self.cfg.amo_webhook_key or not hmac.compare_digest(key, self.cfg.amo_webhook_key):
            return 403, "forbidden"
        if not self.amo.enabled:
            return 200, "amo off"
        added = self.amo.accept_webhook(body)
        if added:
            log(f"[amo] новых сделок в очереди: {added}")
        return 200, "ok"

    # --- экономия места: храним только то, что нужно ---

    def cleanup(self, now=None):
        """messages нужны только для дедупа (Meta повторяет доставку до 7 дней), кэш телефонов — на месяц.
        Обращения, названия объявлений и итоги по сделкам amoCRM не удаляются — это и есть данные дашборда."""
        now = int(now if now is not None else time.time())
        with self.db.transaction() as c:
            msgs = c.execute("DELETE FROM messages WHERE ts < ?", (now - MESSAGES_KEEP_DAYS * 86400,)).rowcount
            contacts = c.execute("DELETE FROM amo_contacts WHERE fetched_at < ?",
                                 (now - AMO_CONTACTS_KEEP_DAYS * 86400,)).rowcount
        if msgs or contacts:
            log(f"[cleanup] удалено старых сообщений: {msgs}, кэша контактов: {contacts}")
        return msgs, contacts

    def start_cleanup(self, interval=6 * 3600):
        def loop():
            while True:
                try:
                    self.cleanup()
                except Exception as e:
                    log(f"[cleanup] ошибка: {type(e).__name__}")
                time.sleep(interval)

        threading.Thread(target=loop, name="cleanup", daemon=True).start()

    # --- вход в дашборд ---

    def login_allowed(self, ip, now=None):
        now = now if now is not None else time.time()
        with self._fails_lock:
            count, first = self._fails.get(ip, (0, now))
            if now - first > LOGIN_WINDOW:
                self._fails.pop(ip, None)
                return True
            return count < LOGIN_MAX_FAILS

    def try_login(self, ip, password, now=None):
        now = now if now is not None else time.time()
        if not self.login_allowed(ip, now):
            return None
        if self.cfg.dashboard_password and hmac.compare_digest(
                password.encode(), self.cfg.dashboard_password.encode()):
            with self._fails_lock:
                self._fails.pop(ip, None)
            return make_session(self.cfg.session_secret, self.cfg.session_ttl_days, now)
        with self._fails_lock:
            count, first = self._fails.get(ip, (0, now))
            self._fails[ip] = (count + 1, first)
        return ""

    def authorized(self, cookie_header):
        if self.cfg.demo_mode:  # демо: данные выдуманные, пароль не нужен
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(cookie_header or "")
        except cookies.CookieError:
            return False
        morsel = jar.get(COOKIE_NAME)
        return bool(morsel) and session_valid(self.cfg.session_secret, morsel.value)

    # --- данные дашборда ---

    def today(self):
        tz = timezone(timedelta(hours=self.cfg.tz_offset_hours))
        return datetime.now(tz).date()

    def report(self, params):
        today = self.today()
        date_from = params.get("from") or (today - timedelta(days=6)).isoformat()
        date_to = params.get("to") or today.isoformat()
        rep = build_report(self.db, date_from, date_to, self.cfg.tz_offset_hours,
                           source=params.get("source", "all"), creative=params.get("creative", ""))
        rep["today"] = today.isoformat()
        rep["status"] = self.status()
        return rep

    def status(self):
        tz = timezone(timedelta(hours=self.cfg.tz_offset_hours))
        last = self.db.query("SELECT MAX(received_at) AS t FROM messages")[0]["t"]
        names = self.db.query(
            "SELECT COUNT(DISTINCT i.ad_id) AS ads,"
            " COUNT(DISTINCT CASE WHEN m.ad_name != '' THEN i.ad_id END) AS named"
            " FROM inquiries i LEFT JOIN ad_meta m ON m.ad_id = i.ad_id WHERE i.ad_id != ''")[0]
        return {
            "last_message_at": datetime.fromtimestamp(last, tz).strftime("%Y-%m-%d %H:%M") if last else None,
            "ads_total": names["ads"], "ads_named": names["named"],
            "ad_names_enabled": self.fetcher.enabled,
            "phone_number_id": self.cfg.phone_number_id,
            "amo_mode": "demo" if self.cfg.demo_mode else (self.cfg.amo_mode if self.amo.enabled else "off"),
            "demo": self.cfg.demo_mode,
            "amo_domain": "" if self.cfg.demo_mode else self.cfg.amo_domain,  # в демо ссылок на amoCRM нет
            "amo_counts": self.amo.summary(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "crm-ads-dashboard"
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    # В лог — только метод и путь: в query бывают verify_token и прочее.
    def log_request(self, code="-", size="-"):
        path = urlsplit(self.path).path
        if path in QUIET_PATHS and str(code).startswith("2"):
            return
        log(f"{self.client_ip()} {self.command} {path} {code}")

    def log_message(self, fmt, *args):
        pass

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def send(self, status, body, ctype="text/plain; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, location, headers=None):
        self.send(303, "", headers=dict(headers or {}, Location=location))

    def params(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def page(self, name, replacements=None):
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        for k, v in (replacements or {}).items():
            html = html.replace("{{" + k + "}}", v)
        return html

    def cookie(self, value, max_age):
        secure = "; Secure" if self.app.cfg.cookie_secure else ""
        return f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self.send(200, "ok")
        if path == "/webhook":
            return self.send(*self.app.verify_webhook(self.params()))
        if path == "/login":
            if self.app.cfg.demo_mode:
                return self.redirect("/")
            return self.send(200, self.page("login.html", {"error": ""}), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name in STATIC_FILES:
                return self.send(200, (WEB_DIR / name).read_bytes(), STATIC_FILES[name])
            return self.send(404, "not found")

        authed = self.app.authorized(self.headers.get("Cookie"))
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": self.cookie("", 0)})
        if path == "/":
            if not authed:
                return self.redirect("/login")
            return self.send(200, self.page("dashboard.html"), "text/html; charset=utf-8")
        if path == "/api/summary":   # агрегаты для дашборда EduApp Sales (вход по ключу, номеров нет)
            return self.summary()
        if path in ("/api/report", "/api/report.csv"):
            if not authed:
                return self.send(401, json.dumps({"error": "login required"}), "application/json")
            try:
                rep = self.app.report(self.params())
            except ValueError:
                return self.send(400, json.dumps({"error": "bad date"}), "application/json")
            if path.endswith(".csv"):
                fname = f"obrascheniya-7000_{rep['from']}_{rep['to']}.csv"
                return self.send(200, report_csv(rep), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="{fname}"'})
            return self.send(200, json.dumps(rep, ensure_ascii=False), "application/json; charset=utf-8")
        return self.send(404, "not found")

    def summary(self):
        cfg, params = self.app.cfg, self.params()
        origin = (self.headers.get("Origin") or "").rstrip("/")
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin and origin in cfg.summary_origins else {"Vary": "Origin"}
        if not cfg.summary_key:
            return self.send(404, "not found")
        if not hmac.compare_digest(params.get("key", "").encode(), cfg.summary_key.encode()):
            return self.send(403, json.dumps({"error": "key"}), "application/json", cors)
        today = self.app.today()
        try:
            rep = build_summary(self.app.db, params.get("from") or (today - timedelta(days=29)).isoformat(),
                                params.get("to") or today.isoformat(), cfg.tz_offset_hours)
        except ValueError:
            return self.send(400, json.dumps({"error": "bad date"}), "application/json", cors)
        rep["today"] = today.isoformat()
        return self.send(200, json.dumps(rep, ensure_ascii=False), "application/json; charset=utf-8", cors)

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self.read_body()
        if body is None:
            return self.send(413, "too large")
        if path == "/webhook":
            try:
                return self.send(*self.app.receive_webhook(body, self.headers.get("X-Hub-Signature-256", "")))
            except Exception as e:  # 500 → Meta повторит доставку, дедуп по wamid защитит от двойного счёта
                log(f"[webhook] ошибка: {type(e).__name__}: {e}")
                return self.send(500, "error")
        if path == "/amo/webhook":
            try:
                return self.send(*self.app.receive_amo_webhook(self.params(), body))
            except Exception as e:
                log(f"[amo] ошибка вебхука: {type(e).__name__}: {e}")
                return self.send(500, "error")
        if path == "/login":
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            session = self.app.try_login(self.client_ip(), form.get("password", ""))
            if session:
                max_age = self.app.cfg.session_ttl_days * 86400
                return self.redirect("/", {"Set-Cookie": self.cookie(session, max_age)})
            error = ("Слишком много попыток. Подождите 15 минут." if session is None
                     else "Неверный пароль.")
            return self.send(401 if session == "" else 429,
                             self.page("login.html", {"error": error}), "text/html; charset=utf-8")
        return self.send(404, "not found")


def make_server(app, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app
    return server


def start_demo(cfg):
    """DEMO_MODE: база в памяти + ~90 дней случайных обращений. Внешние API выключены."""
    import os
    from demo_data import seed
    app = App(cfg)
    started = time.time()
    seed_value = os.environ.get("DEMO_SEED")
    n = seed(app.db, cfg, days=int(os.environ.get("DEMO_DAYS", "90")),
             seed=int(seed_value) if seed_value else None)
    log(f"ДЕМО-РЕЖИМ: сгенерировано {n} случайных обращений за {time.time() - started:.1f} c. "
        "Все данные выдуманы; пароль не нужен. Боевой режим: DEMO_MODE=0 в .env.")
    return app


def main():
    cfg = load_config()
    if cfg.demo_mode:
        app = start_demo(cfg)
        server = make_server(app, cfg.host, cfg.port)
        log(f"Дашборд (демо): http://{cfg.host}:{cfg.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    if not cfg.dashboard_password:
        sys.exit("DASHBOARD_PASSWORD не задан — без пароля дашборд не запускаю.")
    if not cfg.app_secret:
        if cfg.host not in ("127.0.0.1", "localhost"):
            sys.exit("APP_SECRET не задан — на сервере без проверки подписи вебхука не запускаю.")
        log("ВНИМАНИЕ: APP_SECRET не задан, подпись вебхука не проверяется (только для локальной проверки).")
    if not cfg.verify_token:
        log("ВНИМАНИЕ: VERIFY_TOKEN не задан — Meta не сможет подтвердить вебхук.")

    app = App(cfg)
    app.start_cleanup()
    if app.fetcher.enabled:
        app.fetcher.start()
    else:
        log("Названия объявлений из Meta не подтягиваются (нет ACCESS_TOKEN) — креативы по заголовку рекламы.")
    if app.amo.enabled:
        if not cfg.amo_webhook_key:
            log("ВНИМАНИЕ: AMO_WEBHOOK_KEY не задан — вебхук amoCRM принимать не будем.")
        app.amo.start()
        log(f"amoCRM: режим {cfg.amo_mode} ({'пишем в amoCRM' if cfg.amo_mode == 'on' else 'только читаем и журналируем'})")
    else:
        log("amoCRM: выключено")
    server = make_server(app, cfg.host, cfg.port)
    log(f"Номер: phone_number_id {cfg.phone_number_id}")
    log(f"Дашборд: http://{cfg.host}:{cfg.port}/   вебхук: POST /webhook")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.fetcher.stop()
        app.amo.stop()
        server.server_close()


if __name__ == "__main__":
    main()
