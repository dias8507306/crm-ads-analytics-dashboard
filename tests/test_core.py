"""Атрибуция, отчёт и названия объявлений. Запуск: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samples as s  # noqa: E402
from attribution import extract_messages, record_message  # noqa: E402
from config import load_config  # noqa: E402
from db import Database  # noqa: E402
from meta_ads import MAX_ATTEMPTS, AdNameFetcher, GraphError  # noqa: E402
from report import build_report, period_bounds, report_csv  # noqa: E402

DAY = 86400
T0 = 1760000000  # 2025-10-09 08:53 UTC
GAP_DAYS = 30


def ingest(db, payload, number=s.NUMBER_7000):
    return [record_message(db, m, GAP_DAYS, now=T0) for m in extract_messages(payload, number)]


def inquiries(db):
    return db.query("SELECT * FROM inquiries ORDER BY id")


class ExtractTest(unittest.TestCase):
    def test_only_7000_messages(self):
        p2000 = s.webhook([s.message("wamid.1", "77000000001", T0)], phone_number_id=s.NUMBER_2000)
        self.assertEqual(extract_messages(p2000, s.NUMBER_7000), [])
        p7000 = s.webhook([s.message("wamid.2", "77000000001", T0)])
        self.assertEqual(len(extract_messages(p7000, s.NUMBER_7000)), 1)

    def test_statuses_and_reactions_ignored(self):
        self.assertEqual(extract_messages(s.status_update(), s.NUMBER_7000), [])
        p = s.webhook([s.message("wamid.r", "77000000001", T0, kind="reaction")])
        self.assertEqual(extract_messages(p, s.NUMBER_7000), [])

    def test_fields(self):
        ref = s.referral("120200001", "Курс английского")
        m = extract_messages(s.webhook([s.message("wamid.1", "77000000001", T0, "Хочу узнать", ref)],
                                       name="Айгерим"), s.NUMBER_7000)[0]
        self.assertEqual((m.wamid, m.wa_id, m.ts, m.text, m.profile_name),
                         ("wamid.1", "77000000001", T0, "Хочу узнать", "Айгерим"))
        self.assertEqual(m.referral["source_id"], "120200001")

    def test_media_caption(self):
        m = extract_messages(s.webhook([s.message("wamid.1", "7701", T0, "скрин", kind="image")]),
                             s.NUMBER_7000)[0]
        self.assertEqual(m.text, "[фото] скрин")

    def test_garbage(self):
        self.assertEqual(extract_messages(None, s.NUMBER_7000), [])
        self.assertEqual(extract_messages({"entry": [{"changes": [{"field": "messages"}]}]}, s.NUMBER_7000), [])


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_organic_first_message_is_saved(self):
        self.assertEqual(ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0)])),
                         ["new_organic"])
        row = inquiries(self.db)[0]
        self.assertEqual((row["source"], row["ad_id"], row["ctwa_clid"]), ("organic", "", ""))

    def test_ad_message(self):
        ref = s.referral("120200001", "Курс английского", source_url="https://fb.me/x")
        self.assertEqual(ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=ref)])),
                         ["new_ad"])
        row = inquiries(self.db)[0]
        self.assertEqual((row["source"], row["ad_id"], row["headline"], row["source_url"], row["ctwa_clid"]),
                         ("ad", "120200001", "Курс английского", "https://fb.me/x", "ARclid"))

    def test_referral_without_ctwa_clid_is_still_ad(self):
        ref = s.referral("120200001", "Курс", ctwa_clid=None)
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=ref)]))
        row = inquiries(self.db)[0]
        self.assertEqual((row["source"], row["ctwa_clid"]), ("ad", ""))

    def test_duplicate_delivery_counted_once(self):
        p = s.webhook([s.message("wamid.1", "77000000001", T0)])
        self.assertEqual(ingest(self.db, p), ["new_organic"])
        self.assertEqual(ingest(self.db, p), ["duplicate"])
        self.assertEqual(len(inquiries(self.db)), 1)
        self.assertEqual(inquiries(self.db)[0]["messages"], 1)

    def test_follow_up_messages_join_inquiry(self):
        ref = s.referral("120200001", "Курс")
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=ref)]))
        ingest(self.db, s.webhook([s.message("wamid.2", "77000000001", T0 + 60, "А сколько стоит?")]))
        ingest(self.db, s.webhook([s.message("wamid.3", "77000000001", T0 + 5 * DAY)]))
        rows = inquiries(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["messages"], rows[0]["last_at"]), ("ad", 3, T0 + 5 * DAY))

    def test_after_long_silence_new_organic_inquiry(self):
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=s.referral("1", "A"))]))
        self.assertEqual(ingest(self.db, s.webhook([s.message("wamid.2", "77000000001", T0 + 31 * DAY)])),
                         ["new_organic"])
        self.assertEqual([r["source"] for r in inquiries(self.db)], ["ad", "organic"])

    def test_ad_click_inside_organic_inquiry_starts_ad_inquiry(self):
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0)]))
        self.assertEqual(ingest(self.db, s.webhook([s.message(
            "wamid.2", "77000000001", T0 + 3600, referral=s.referral("120200001", "Курс"))])), ["new_ad"])
        self.assertEqual([r["source"] for r in inquiries(self.db)], ["organic", "ad"])

    def test_same_ad_clicked_again_is_same_inquiry(self):
        ref = s.referral("120200001", "Курс")
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=ref)]))
        self.assertEqual(ingest(self.db, s.webhook([s.message("wamid.2", "77000000001", T0 + DAY, referral=ref)])),
                         ["appended"])
        self.assertEqual(len(inquiries(self.db)), 1)

    def test_other_ad_clicked_is_new_inquiry(self):
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0, referral=s.referral("1", "A"))]))
        self.assertEqual(ingest(self.db, s.webhook([s.message(
            "wamid.2", "77000000001", T0 + DAY, referral=s.referral("2", "B"))])), ["new_ad"])
        self.assertEqual([r["ad_id"] for r in inquiries(self.db)], ["1", "2"])

    def test_different_people_separate(self):
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0)]))
        ingest(self.db, s.webhook([s.message("wamid.2", "77000000002", T0)]))
        self.assertEqual(len(inquiries(self.db)), 2)

    def test_2000_not_saved(self):
        ingest(self.db, s.webhook([s.message("wamid.1", "77000000001", T0)], phone_number_id=s.NUMBER_2000))
        self.assertEqual(inquiries(self.db), [])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        a, b = s.referral("111", "Сторис: скидка"), s.referral("222", "Сторис: учитель")
        n = 0
        for ref, count in ((a, 3), (b, 1), (None, 2)):
            for _ in range(count):
                n += 1
                ingest(self.db, s.webhook([s.message(f"wamid.{n}", f"7701000000{n}", T0 + n, referral=ref)]))
        self.d = "2025-10-09"

    def test_totals_and_summary(self):
        rep = build_report(self.db, self.d, self.d, 5)
        self.assertEqual(rep["totals"], {"all": 6, "ad": 4, "organic": 2})
        self.assertEqual([(c["label"], c["count"]) for c in rep["creatives"]],
                         [("Сторис: скидка", 3), ("Сторис: учитель", 1), ("Органика (без рекламы)", 2)])

    def test_ad_name_wins_over_headline(self):
        self.db.execute("INSERT INTO ad_meta (ad_id, ad_name, campaign_name, fetched_at) "
                        "VALUES ('111', 'Видео скидка v2', 'Осень 2026', 1)")
        rep = build_report(self.db, self.d, self.d, 5)
        top = rep["creatives"][0]
        self.assertEqual((top["label"], top["campaign_name"], top["headline"]),
                         ("Видео скидка v2", "Осень 2026", "Сторис: скидка"))

    def test_filters(self):
        self.assertEqual(build_report(self.db, self.d, self.d, 5, source="organic")["inquiries_total"], 2)
        self.assertEqual(build_report(self.db, self.d, self.d, 5, creative="ad:111")["inquiries_total"], 3)

    def test_period_outside(self):
        self.assertEqual(build_report(self.db, "2025-10-10", "2025-10-11", 5)["totals"]["all"], 0)

    def test_period_bounds_local_time(self):
        start, end, _, _ = period_bounds("2026-10-12", "2026-10-12", 5)
        self.assertEqual(end - start, DAY)
        self.assertEqual(start, 1791745200)  # 2026-10-12 00:00 UTC+5 = 2026-10-11 19:00 UTC

    def test_bad_date(self):
        with self.assertRaises(ValueError):
            build_report(self.db, "вчера", self.d, 5)

    def test_platform(self):
        from report import platform_of
        self.assertEqual(platform_of("https://www.instagram.com/p/DPx1/"), "Instagram")
        self.assertEqual(platform_of("https://fb.me/4FKsqqpug"), "Facebook")
        self.assertEqual(platform_of("https://wa.me/wamo/abc"), "WhatsApp")
        self.assertEqual(platform_of("https://evil-instagram.com/x"), "Meta")
        self.assertEqual(platform_of(""), "")
        rep = build_report(self.db, self.d, self.d, 5)
        self.assertEqual(rep["creatives"][0]["platforms"], {"Facebook": 3})  # samples: https://fb.me/ad
        ad_row = next(r for r in rep["inquiries"] if r["source"] == "ad")
        self.assertEqual((ad_row["platform"], ad_row["media"]), ("Facebook", "видео"))
        org_row = next(r for r in rep["inquiries"] if r["source"] == "organic")
        self.assertEqual((org_row["platform"], org_row["media"]), ("", ""))

    def test_csv(self):
        text = report_csv(build_report(self.db, self.d, self.d, 5))
        self.assertTrue(text.startswith("﻿Дата и время;"))
        self.assertIn("Таргет;Facebook;видео;Сторис: скидка", text)
        self.assertEqual(len(text.strip().splitlines()), 7)


class AdNamesTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.cfg = load_config(env_file="nonexistent.env",
                               overrides={"DEMO_MODE": "0", "ACCESS_TOKEN": "tok", "DASHBOARD_PASSWORD": "x"})
        for i, ad in enumerate(("120200001", "120200002", "demo-1")):
            ingest(self.db, s.webhook([s.message(f"wamid.{i}", f"770100000{i}", T0, referral=s.referral(ad, "H"))]))
        self.calls = []

    def fetcher(self, responses):
        def fetch(ad_id):
            self.calls.append(ad_id)
            r = responses[ad_id]
            if isinstance(r, Exception):
                raise r
            return r
        return AdNameFetcher(self.db, self.cfg, fetch=fetch, sleep=lambda _: None)

    def test_fetches_names_once(self):
        f = self.fetcher({
            "120200001": {"name": "Видео 1", "adset": {"name": "Группа"}, "campaign": {"name": "Кампания"}},
            "120200002": {"name": "Видео 2"},
        })
        self.assertEqual(f.run_once(), 2)
        self.assertEqual(sorted(self.calls), ["120200001", "120200002"])  # demo-1 в Meta не уходит
        meta = {r["ad_id"]: r for r in self.db.query("SELECT * FROM ad_meta")}
        self.assertEqual((meta["120200001"]["ad_name"], meta["120200001"]["adset_name"],
                          meta["120200001"]["campaign_name"]), ("Видео 1", "Группа", "Кампания"))
        self.calls.clear()
        self.assertEqual(f.run_once(), 0)
        self.assertEqual(self.calls, [])  # повторно не спрашиваем

    def test_errors_back_off_and_give_up(self):
        f = self.fetcher({"120200001": GraphError("HTTP 400: no permission"), "120200002": {"name": "ok"}})
        f.run_once()
        row = self.db.query("SELECT * FROM ad_meta WHERE ad_id = '120200001'")[0]
        self.assertEqual((row["attempts"], row["ad_name"]), (1, ""))
        self.assertIn("no permission", row["last_error"])
        t = row["fetched_at"]
        self.assertNotIn("120200001", f.pending_ad_ids(now=t + 10))       # сразу не повторяем
        self.assertIn("120200001", f.pending_ad_ids(now=t + 3601))        # через час — ещё раз
        self.db.execute("UPDATE ad_meta SET attempts = ? WHERE ad_id = '120200001'", (MAX_ATTEMPTS,))
        self.assertNotIn("120200001", f.pending_ad_ids(now=t + 10 * DAY))  # после MAX_ATTEMPTS — сдались

    def test_disabled_without_token(self):
        cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "0", "ACCESS_TOKEN": "", "DASHBOARD_PASSWORD": "x"})
        self.assertEqual(AdNameFetcher(self.db, cfg, fetch=lambda _: 1 / 0).run_once(), 0)


if __name__ == "__main__":
    unittest.main()
