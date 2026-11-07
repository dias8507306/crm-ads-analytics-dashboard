"""HTTP: вебхук (проверка Meta, подпись), вход, доступ к отчёту. Сервер поднимается на свободном порту."""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samples as s  # noqa: E402
from app import App, make_server, make_session, session_valid  # noqa: E402
from config import load_config  # noqa: E402
from db import Database  # noqa: E402

SECRET = "test-app-secret"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "0", 
            "APP_SECRET": SECRET, "VERIFY_TOKEN": "verify-me", "DASHBOARD_PASSWORD": "pass-123",
            "ACCESS_TOKEN": "", "DB_PATH": ":memory:",
            "SUMMARY_KEY": "sum-key-123", "SUMMARY_ORIGINS": "https://eduapp-sales.example.com"})
        cls.app = App(cls.cfg, db=Database(":memory:"))
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def post_webhook(self, payload, signature=None):
        body = s.encode(payload)
        sig = s.sign(SECRET, body) if signature is None else signature
        return self.request("POST", "/webhook", body, {"Content-Type": "application/json",
                                                       "X-Hub-Signature-256": sig})

    def login(self):
        resp, _ = self.request("POST", "/login", urlencode({"password": "pass-123"}),
                               {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 303)
        return resp.getheader("Set-Cookie").split(";")[0]

    def test_health(self):
        resp, data = self.request("GET", "/health")
        self.assertEqual((resp.status, data), (200, b"ok"))

    def test_verify_webhook(self):
        q = urlencode({"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
        resp, data = self.request("GET", "/webhook?" + q)
        self.assertEqual((resp.status, data), (200, b"12345"))
        q = urlencode({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
        self.assertEqual(self.request("GET", "/webhook?" + q)[0].status, 403)

    def test_signature_required(self):
        payload = s.webhook([s.message("wamid.sig", "77000009999", 1760000000)])
        self.assertEqual(self.post_webhook(payload, signature="sha256=deadbeef")[0].status, 403)
        self.assertEqual(self.post_webhook(payload, signature="")[0].status, 403)
        self.assertEqual(self.app.db.query("SELECT COUNT(*) AS n FROM messages WHERE wamid='wamid.sig'")[0]["n"], 0)
        self.assertEqual(self.post_webhook(payload)[0].status, 200)
        self.assertEqual(self.app.db.query("SELECT COUNT(*) AS n FROM messages WHERE wamid='wamid.sig'")[0]["n"], 1)

    def test_bad_json(self):
        body = b"{not json"
        resp, _ = self.request("POST", "/webhook", body, {"X-Hub-Signature-256": s.sign(SECRET, body)})
        self.assertEqual(resp.status, 400)

    def test_dashboard_requires_login(self):
        resp, _ = self.request("GET", "/")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/login"))
        self.assertEqual(self.request("GET", "/api/report")[0].status, 401)
        self.assertEqual(self.request("GET", "/api/report.csv")[0].status, 401)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": "d7000_session=v1.9999999999.forged"})[0].status, 303)

    def test_wrong_password(self):
        resp, data = self.request("POST", "/login", urlencode({"password": "nope"}),
                                  {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 401)
        self.assertIn("Неверный пароль", data.decode("utf-8"))

    def test_report_flow(self):
        ts = 1760000000  # 2025-10-09 по UTC+5
        self.post_webhook(s.webhook([s.message("wamid.r1", "77000001111", ts, "Привет",
                                               s.referral("120209999", "Сторис: тест"))]))
        self.post_webhook(s.webhook([s.message("wamid.r2", "77000002222", ts + 5, "Добрый день")]))
        self.post_webhook(s.webhook([s.message("wamid.r3", "77000003333", ts)], phone_number_id=s.NUMBER_2000))
        cookie = self.login()
        resp, data = self.request("GET", "/api/report?from=2025-10-09&to=2025-10-09", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200)
        rep = json.loads(data)
        self.assertEqual(rep["totals"], {"all": 2, "ad": 1, "organic": 1})
        self.assertEqual(rep["creatives"][0]["label"], "Сторис: тест")
        self.assertNotIn("77000003333", data.decode("utf-8"))  # 2000 не пишем
        resp, data = self.request("GET", "/api/report.csv?from=2025-10-09&to=2025-10-09", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        resp, html = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200)
        self.assertIn("Content-Security-Policy", dict(resp.getheaders()))
        self.assertEqual(self.request("GET", "/api/report?from=bad", headers={"Cookie": cookie})[0].status, 400)

    def test_summary_aggregates_only(self):
        ts = 1760100000  # 2025-10-10 по UTC+5
        self.post_webhook(s.webhook([s.message("wamid.s1", "77000004444", ts, "Сәлем",
                                               s.referral("120208888", "Видео: олимпиада"))]))
        self.post_webhook(s.webhook([s.message("wamid.s2", "77000005555", ts + 9, "Қайырлы күн")]))
        q = "/api/summary?from=2025-10-10&to=2025-10-10"
        self.assertEqual(self.request("GET", q)[0].status, 403)                       # без ключа
        self.assertEqual(self.request("GET", q + "&key=wrong")[0].status, 403)
        resp, data = self.request("GET", q + "&key=sum-key-123", headers={"Origin": "https://eduapp-sales.example.com"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "https://eduapp-sales.example.com")
        rep = json.loads(data)
        self.assertEqual(rep["totals"], {"all": 2, "ad": 1, "organic": 1})
        self.assertEqual(rep["days"], [{"date": "2025-10-10", "ad": 1, "organic": 1}])
        self.assertEqual(rep["creatives"][0]["count"], 1)
        text = data.decode("utf-8")
        for secret in ("77000004444", "77000005555", "Сәлем", "Қайырлы күн"):
            self.assertNotIn(secret, text)                                              # номеров и текстов нет
        resp, _ = self.request("GET", q + "&key=sum-key-123", headers={"Origin": "https://evil.example"})
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.request("GET", "/api/summary?key=sum-key-123&from=bad")[0].status, 400)

    def test_static_whitelist(self):
        self.assertEqual(self.request("GET", "/static/style.css")[0].status, 200)
        self.assertEqual(self.request("GET", "/static/../config.py")[0].status, 404)
        self.assertEqual(self.request("GET", "/static/login.html")[0].status, 404)


class SessionTest(unittest.TestCase):
    def test_session(self):
        tok = make_session(b"k", 30, now=1000)
        self.assertTrue(session_valid(b"k", tok, now=1001))
        self.assertFalse(session_valid(b"other", tok, now=1001))
        self.assertFalse(session_valid(b"k", tok, now=1000 + 31 * 86400))
        self.assertFalse(session_valid(b"k", "garbage"))


class CleanupTest(unittest.TestCase):
    def test_old_messages_pruned_inquiries_kept(self):
        from attribution import extract_messages, record_message
        cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "0", "DASHBOARD_PASSWORD": "x", "DB_PATH": ":memory:"})
        app = App(cfg, db=Database(":memory:"))
        now = 1791800000
        for wamid, ts in (("wamid.old", now - 20 * 86400), ("wamid.new", now - 86400)):
            for m in extract_messages(s.webhook([s.message(wamid, "77000001122", ts, "x" * 1000)]), s.NUMBER_7000):
                record_message(app.db, m, 30, now=ts)
        app.db.execute("INSERT INTO amo_contacts (contact_id, phones, fetched_at) VALUES (1, '7701', ?)",
                       (now - 40 * 86400,))
        self.assertEqual(app.cleanup(now=now), (1, 1))
        self.assertEqual([r["wamid"] for r in app.db.query("SELECT wamid FROM messages")], ["wamid.new"])
        rows = app.db.query("SELECT first_text FROM inquiries")
        self.assertEqual(len(rows), 1)                    # обращение не удалено (второе сообщение — то же обращение)
        self.assertEqual(len(rows[0]["first_text"]), 300)  # храним только начало текста


class LoginThrottleTest(unittest.TestCase):
    def test_lockout(self):
        cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "0", "DASHBOARD_PASSWORD": "right", "DB_PATH": ":memory:"})
        app = App(cfg, db=Database(":memory:"))
        for _ in range(5):
            self.assertEqual(app.try_login("1.2.3.4", "wrong", now=100), "")
        self.assertIsNone(app.try_login("1.2.3.4", "right", now=101))          # заблокирован
        self.assertTrue(app.try_login("5.6.7.8", "right", now=101))            # другой IP — можно
        self.assertTrue(app.try_login("1.2.3.4", "right", now=100 + 16 * 60))  # окно прошло


if __name__ == "__main__":
    unittest.main()
