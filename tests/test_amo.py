"""amoCRM: вебхук, сопоставление по телефону, что пишем, лимитер. Настоящая amoCRM не вызывается."""
import sys
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samples as s  # noqa: E402
from amocrm import (MAX_ATTEMPTS, PROCESS_DELAY, REPEAT_GUARD_SECONDS, AmoClient, AmoError,  # noqa: E402
                    AmoSync, contact_phones, normalize_phone, parse_webhook)
from attribution import extract_messages, record_message  # noqa: E402
from config import load_config  # noqa: E402
from db import Database  # noqa: E402

T0 = 1791700000
PIPE_OP = 10000000
SOURCE_FIELD = 1000001
TARGET = 1000002
ORGANIC_WA = 1000003


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


class FakeAmo:
    """Подставная amoCRM: отвечает по заранее заданным сделкам и контактам, пишет журнал вызовов."""

    def __init__(self):
        self.leads = {}
        self.contacts = {}
        self.calls = []
        self.fail = {}

    def add_lead(self, lead_id, contact_id, phone, before_enum=0, before_text="", pipeline=PIPE_OP, created=T0):
        cf = []
        if before_enum:
            cf.append({"field_id": SOURCE_FIELD, "values": [{"value": before_text, "enum_id": before_enum}]})
        self.leads[lead_id] = {"id": lead_id, "pipeline_id": pipeline, "created_at": created,
                               "custom_fields_values": cf,
                               "_embedded": {"contacts": [{"id": contact_id, "is_main": True}]}}
        self.contacts[contact_id] = {"id": contact_id, "custom_fields_values": [
            {"field_code": "PHONE", "values": [{"value": phone, "enum_code": "WORK"}]}]}

    def __call__(self, method, url, body):
        path = "/" + url.split("/", 3)[3]  # https://<домен>/api/v4/... -> /api/v4/...
        self.calls.append((method, path, body))
        if path in self.fail:
            return self.fail.pop(path)
        if method == "GET" and path.startswith("/api/v4/leads/"):
            lead_id = int(path.split("/")[4].split("?")[0])
            return (200, self.leads[lead_id]) if lead_id in self.leads else (404, {"title": "Not found"})
        if method == "GET" and path.startswith("/api/v4/contacts/"):
            return 200, self.contacts[int(path.split("/")[4])]
        if method == "PATCH":
            return 200, {"id": int(path.split("/")[4])}
        return 400, {"title": "unexpected"}


def make(mode="on"):
    db = Database(":memory:")
    cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "0", 
        "AMO_TOKEN": "amo-secret-token", "AMO_MODE": mode, "AMO_WEBHOOK_KEY": "hook-key",
        "DASHBOARD_PASSWORD": "x", "DB_PATH": ":memory:"})
    clock = Clock(T0 + 10)
    fake = FakeAmo()
    client = AmoClient(cfg.amo_domain, cfg.amo_token, transport=fake, clock=clock, sleep=clock.sleep)
    sync = AmoSync(db, cfg, client=client, clock=clock)
    return db, cfg, clock, fake, sync


def inquiry(db, wa_id, ts, ref=None, n=[0]):
    n[0] += 1
    for m in extract_messages(s.webhook([s.message(f"wamid.a{n[0]}", wa_id, ts, referral=ref)]), s.NUMBER_7000):
        record_message(db, m, 30, now=ts)


def amo_hook(*leads, account="30000000"):
    form = {"account[id]": account, "account[subdomain]": "your_amo_subdomain"}
    for i, (lead_id, pipeline) in enumerate(leads):
        form[f"leads[add][{i}][id]"] = str(lead_id)
        form[f"leads[add][{i}][pipeline_id]"] = str(pipeline)
        form[f"leads[add][{i}][status_id]"] = "1"
        form[f"leads[add][{i}][created_at]"] = str(T0)
    return urlencode(form).encode()


def lead_row(db, lead_id):
    return db.query("SELECT * FROM amo_leads WHERE lead_id = ?", (lead_id,))[0]


def run(sync, clock):
    clock.t += PROCESS_DELAY + 1
    return sync.run_once()


class HelpersTest(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(normalize_phone("+7 (700) 000-70-00"), "77000007000")
        self.assertEqual(normalize_phone("87000007000"), "77000007000")
        self.assertEqual(normalize_phone("7000007000"), "77000007000")

    def test_contact_phones(self):
        c = {"custom_fields_values": [{"field_code": "PHONE", "values": [{"value": "+77000001122"},
                                                                         {"value": "8 700 000 11 22"}]},
                                      {"field_code": "EMAIL", "values": [{"value": "a@b.c"}]}]}
        self.assertEqual(contact_phones(c), ["77000001122"])

    def test_parse_webhook(self):
        account, leads = parse_webhook(amo_hook((1, PIPE_OP), (2, 999)))
        self.assertEqual(account, "30000000")
        self.assertEqual(sorted(l["id"] for l in leads), ["1", "2"])


class SyncTest(unittest.TestCase):
    def test_ad_lead_gets_target_and_utm(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0 - 5, s.referral("120200001", "Сторис: скидка",
                                                       source_url="https://www.instagram.com/stories/x"))
        db.execute("INSERT INTO ad_meta (ad_id, ad_name, campaign_name, fetched_at) "
                   "VALUES ('120200001', 'Видео скидка', 'Осень', 1)")
        fake.add_lead(501, 9001, "+7 700 000 11 22")
        self.assertEqual(sync.accept_webhook(amo_hook((501, PIPE_OP))), 1)
        run(sync, clock)
        row = lead_row(db, 501)
        self.assertEqual(row["state"], "written")
        self.assertEqual(row["action"], "Таргет · Видео скидка")
        patch = [c for c in fake.calls if c[0] == "PATCH"]
        self.assertEqual(len(patch), 1)
        values = {f["field_id"]: f["values"][0] for f in patch[0][2]["custom_fields_values"]}
        self.assertEqual(values[SOURCE_FIELD], {"enum_id": TARGET})
        self.assertEqual(values[1000011]["value"], "instagram")   # utm_source
        self.assertEqual(values[1000012]["value"], "ctwa")        # utm_medium
        self.assertEqual(values[1000013]["value"], "Осень")       # utm_campaign
        self.assertEqual(values[1000014]["value"], "Видео скидка")  # utm_content
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET", "PATCH"])

    def test_manager_value_overwritten_for_ad(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0, s.referral("1", "Сторис"))
        fake.add_lead(502, 9002, "77000001122", before_enum=ORGANIC_WA, before_text="Органика WhatsApp")
        sync.accept_webhook(amo_hook((502, PIPE_OP)))
        run(sync, clock)
        row = lead_row(db, 502)
        self.assertEqual(row["state"], "written")
        self.assertEqual(row["action"], "было «Органика WhatsApp» → Таргет · Сторис")

    def test_organic_not_touched(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0)
        fake.add_lead(503, 9003, "77000001122")
        sync.accept_webhook(amo_hook((503, PIPE_OP)))
        run(sync, clock)
        self.assertEqual(lead_row(db, 503)["state"], "organic")
        self.assertNotIn("PATCH", [c[0] for c in fake.calls])

    def test_already_target_no_patch(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0, s.referral("1", "Сторис"))
        fake.add_lead(504, 9004, "77000001122", before_enum=TARGET, before_text="Таргет")
        sync.accept_webhook(amo_hook((504, PIPE_OP)))
        run(sync, clock)
        self.assertEqual(lead_row(db, 504)["state"], "already")
        self.assertNotIn("PATCH", [c[0] for c in fake.calls])

    def test_dry_run_reads_but_never_writes(self):
        db, cfg, clock, fake, sync = make(mode="dry-run")
        inquiry(db, "77000001122", T0, s.referral("1", "Сторис"))
        fake.add_lead(505, 9005, "77000001122")
        sync.accept_webhook(amo_hook((505, PIPE_OP)))
        run(sync, clock)
        self.assertEqual(lead_row(db, 505)["state"], "dry_run")
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET"])

    def test_off_does_nothing(self):
        db, cfg, clock, fake, sync = make(mode="off")
        self.assertFalse(sync.enabled)
        self.assertEqual(run(sync, clock), 0)
        self.assertEqual(fake.calls, [])

    def test_other_pipeline_and_account_ignored(self):
        db, cfg, clock, fake, sync = make()
        self.assertEqual(sync.accept_webhook(amo_hook((506, 999))), 0)
        self.assertEqual(sync.accept_webhook(amo_hook((507, PIPE_OP), account="1")), 0)
        run(sync, clock)
        self.assertEqual(fake.calls, [])

    def test_duplicate_webhook_one_row(self):
        db, cfg, clock, fake, sync = make()
        self.assertEqual(sync.accept_webhook(amo_hook((508, PIPE_OP))), 1)
        self.assertEqual(sync.accept_webhook(amo_hook((508, PIPE_OP))), 0)

    def test_contact_cached(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0)
        fake.add_lead(509, 9009, "77000001122")
        fake.add_lead(510, 9009, "77000001122")
        sync.accept_webhook(amo_hook((509, PIPE_OP), (510, PIPE_OP)))
        run(sync, clock)
        contact_gets = [c for c in fake.calls if c[1].startswith("/api/v4/contacts/")]
        self.assertEqual(len(contact_gets), 1)

    def test_waits_for_meta_webhook_then_matches_locally(self):
        db, cfg, clock, fake, sync = make()
        fake.add_lead(511, 9011, "77000001122")
        sync.accept_webhook(amo_hook((511, PIPE_OP)))
        run(sync, clock)                       # обращения ещё нет
        self.assertEqual(lead_row(db, 511)["state"], "fetched")
        inquiry(db, "77000001122", T0 + 30, s.referral("1", "Сторис"))
        clock.t += 200
        sync.run_once()
        self.assertEqual(lead_row(db, 511)["state"], "written")
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET", "PATCH"])  # amoCRM повторно не читали

    def test_no_match_after_wait(self):
        db, cfg, clock, fake, sync = make()
        fake.add_lead(512, 9012, "77000009999")
        sync.accept_webhook(amo_hook((512, PIPE_OP)))
        run(sync, clock)
        clock.t += 31 * 60
        sync.run_once()
        self.assertEqual(lead_row(db, 512)["state"], "no_match")

    def test_old_inquiry_not_matched(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0 - 5 * 86400, s.referral("1", "Сторис"))  # давно и молчит
        fake.add_lead(513, 9013, "77000001122")
        sync.accept_webhook(amo_hook((513, PIPE_OP)))
        run(sync, clock)
        self.assertEqual(lead_row(db, 513)["state"], "fetched")

    def test_deleted_lead(self):
        db, cfg, clock, fake, sync = make()
        sync.accept_webhook(amo_hook((514, PIPE_OP)))
        run(sync, clock)
        self.assertEqual(lead_row(db, 514)["state"], "gone")

    def test_utm_rejected_falls_back(self):
        db, cfg, clock, fake, sync = make()
        inquiry(db, "77000001122", T0, s.referral("1", "Сторис"))
        fake.add_lead(515, 9015, "77000001122")
        orig = fake.__call__

        def reject_utm(method, url, body):
            if method == "PATCH" and len(body["custom_fields_values"]) > 1:
                fake.calls.append((method, url, body))
                return 400, {"title": "Bad Request", "validation-errors": [{"code": "x"}]}
            return orig(method, url, body)
        sync.client._transport = reject_utm
        sync.accept_webhook(amo_hook((515, PIPE_OP)))
        run(sync, clock)
        row = lead_row(db, 515)
        self.assertEqual((row["state"], row["action"]), ("written", "Таргет · Сторис (без utm)"))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(T0)
        self.fake = FakeAmo()
        self.fake.add_lead(1, 2, "77000001122")
        self.client = AmoClient("demo.amocrm.test", "amo-secret-token", transport=self.fake,
                                clock=self.clock, sleep=self.clock.sleep)

    def test_rate_limited(self):
        start = self.clock.t
        self.client.request("GET", "/api/v4/leads/1?with=contacts")
        self.client.request("GET", "/api/v4/contacts/2")
        self.client.request("PATCH", "/api/v4/leads/1", {"a": 1})
        self.assertGreaterEqual(self.clock.t - start, 1.0)  # 3 запроса — минимум 2 интервала по 0.5 с

    def test_no_repeat_of_same_request(self):
        self.client.request("GET", "/api/v4/contacts/2")
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/contacts/2")
        self.assertEqual(len(self.fake.calls), 1)
        self.clock.t += REPEAT_GUARD_SECONDS + 1
        self.client.request("GET", "/api/v4/contacts/2")
        self.assertEqual(len(self.fake.calls), 2)

    def test_max_attempts(self):
        for _ in range(MAX_ATTEMPTS):
            self.client.request("GET", "/api/v4/contacts/2")
            self.clock.t += REPEAT_GUARD_SECONDS + 1
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/contacts/2")

    def test_403_stops_everything(self):
        self.fake.fail["/api/v4/contacts/2"] = (403, {"title": "Forbidden"})
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/contacts/2")
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/leads/1?with=contacts")
        self.assertEqual(len(self.fake.calls), 1)

    def test_429_pauses(self):
        self.fake.fail["/api/v4/contacts/2"] = (429, {"title": "Too Many Requests"})
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/contacts/2")
        with self.assertRaises(AmoError):
            self.client.request("GET", "/api/v4/leads/1?with=contacts")
        self.clock.t += 121
        self.client.request("GET", "/api/v4/leads/1?with=contacts")

    def test_token_not_in_errors(self):
        self.fake.fail["/api/v4/contacts/2"] = (400, {"title": "bad amo-secret-token"})
        with self.assertRaises(AmoError) as ctx:
            self.client.request("GET", "/api/v4/contacts/2")
        self.assertNotIn("amo-secret-token", str(ctx.exception))


class WebhookEndpointTest(unittest.TestCase):
    def test_key_required(self):
        from app import App
        db, cfg, clock, fake, sync = make()
        app = App(cfg, db=db, amo=sync)
        body = amo_hook((520, PIPE_OP))
        self.assertEqual(app.receive_amo_webhook({"key": "wrong"}, body)[0], 403)
        self.assertEqual(app.receive_amo_webhook({}, body)[0], 403)
        self.assertEqual(app.receive_amo_webhook({"key": "hook-key"}, body), (200, "ok"))
        self.assertEqual(lead_row(db, 520)["state"], "new")


if __name__ == "__main__":
    unittest.main()
