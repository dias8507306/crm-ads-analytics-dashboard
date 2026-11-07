"""Demo data generator: random but plausible WhatsApp inquiries, ad names and amoCRM deals.

Used by `python app.py` in DEMO_MODE (the default): the app starts with an in-memory database
and fills it with ~90 days of generated traffic, so the dashboard works offline with no keys.

Messages go through the same pipeline as real webhooks (samples.webhook -> extract_messages ->
record_message), so the "what counts as an inquiry" rules are exercised by the demo too.

Shape of the data:
  * weekday seasonality (Mon-Thu busy, weekend quiet) and a slow upward trend;
  * two daily peaks (late morning and evening, local time UTC+5);
  * ad campaigns that start/stop over the period, each with several creatives of different strength;
  * ~10% of ad clicks without ctwa_clid, some returning clients, 0-5 follow-up messages;
  * ~85% of inquiries get an amoCRM deal with a final state (dry-run / written / already / organic).

Every name, number and text here is made up.

CLI (writes a SQLite file instead of memory, e.g. to run the app with DEMO_MODE=0 against it):
    python demo_data.py --db data/demo.db --days 90 --seed 42
"""
import argparse
import math
import random
import time
from datetime import datetime, timedelta, timezone

import samples as s
from attribution import extract_messages, record_message

# (ad_id, headline, ad name in Meta, campaign, ad set, media, platform, weight, first_day, last_day)
# first_day / last_day are offsets from the start of the period as a fraction (0..1).
CAMPAIGNS = [
    ("120210000000000101", "Онлайн-мектеп 3–9 сынып", "Видео: мұғаліммен танысу", "Онлайн-мектеп · күз",
     "KZ · ата-аналар 30-45", "video", "instagram", 9, 0.0, 1.0),
    ("120210000000000102", "Онлайн-мектеп: тегін сынақ сабақ", "Сторис: тегін сынақ сабақ", "Онлайн-мектеп · күз",
     "KZ · ата-аналар 30-45", "image", "instagram", 6, 0.0, 1.0),
    ("120210000000000103", "Ағылшын тілі 5–11 сынып", "Reels: ағылшын тілі", "EduApp English",
     "Алматы + Астана", "video", "instagram", 7, 0.15, 1.0),
    ("120210000000000104", "English: speaking club", "Карусель: speaking club", "EduApp English",
     "Алматы + Астана", "image", "facebook", 3, 0.15, 0.8),
    ("120210000000000105", "ҰБТ-ға дайындық", "Видео: ҰБТ 140+ балл", "ҰБТ · 11 сынып",
     "KZ · 16-18 + ата-аналар", "video", "instagram", 8, 0.35, 1.0),
    ("120210000000000106", "ҰБТ: пробный тест бесплатно", "Сторис: пробный тест", "ҰБТ · 11 сынып",
     "KZ · 16-18 + ата-аналар", "image", "facebook", 4, 0.35, 1.0),
    ("120210000000000107", "НИШ / БИЛ дайындық", "Видео: НЗМ-ге дайындық", "НИШ / БИЛ · көктем",
     "KZ · ата-аналар 4-6 сынып", "video", "instagram", 5, 0.0, 0.55),
    ("120210000000000108", "Олимпиада: Алтын сақа", "Сторис: олимпиадаға дайындық", "НИШ / БИЛ · көктем",
     "KZ · ата-аналар 4-6 сынып", "image", "instagram", 2, 0.6, 1.0),
]
# This one has no name from Meta yet: the dashboard falls back to the referral headline.
UNNAMED_AD = ("120210000000000199", "Жаңа топтар: қыркүйек", "video", "instagram", 2, 0.85, 1.0)

FIRST_NAMES = ["Айгерім", "Ерлан", "Динара", "Нұрлан", "Әсел", "Мадина", "Арман", "Жанар", "Бауыржан",
               "Сәуле", "Данияр", "Камила", "Ержан", "Гүлнара", "Айдана", "Тимур", "Әлия", "Ақбота",
               "Самат", "Меруерт", "Нұргүл", "Олжас", "Дана", "Ғалым", "Inna", "Aru", "Mama Aliya"]
FIRST_TEXTS = ["Здравствуйте! Хочу узнать подробнее", "Сәлеметсіз бе! Толығырақ айтып беріңізші",
               "Сколько стоит обучение?", "Добрый день, есть места на курс?", "Сәлем, бағасы қанша?",
               "Здравствуйте, ребёнок в 7 классе, подойдёт?", "Қай уақытта сабақ болады?",
               "Хочу записаться на пробный урок", "Тегін сабаққа жазылғым келеді", "[фото]",
               "Можно подробнее про подготовку к ЕНТ?", "Ағылшын тілі курсы бар ма?"]
FOLLOW_UPS = ["А какое расписание?", "Ребёнку 12 лет", "Рахмет!", "Можно пробный урок?", "Хорошо, жду",
              "Онлайн ба, офлайн ба?", "Сколько уроков в неделю?", "Түсінікті, рахмет", "[голосовое]"]

WEEKDAY = [1.15, 1.12, 1.05, 1.0, 0.92, 0.68, 0.58]          # Mon..Sun
HOUR_WEIGHTS = [0.1, 0.05, 0.02, 0.02, 0.03, 0.1, 0.3, 0.6, 1.0, 1.4, 1.7, 1.8, 1.6, 1.3, 1.2, 1.2,
                1.3, 1.4, 1.6, 1.9, 2.1, 1.9, 1.2, 0.5]


def _phone(rnd, used):
    """+7 700 000 XX XX — obviously fake numbers, unique within one demo run."""
    for _ in range(100):
        p = "7700000" + "".join(rnd.choice("0123456789") for _ in range(4))
        if p not in used:
            used.add(p)
            return p
    return p


def generate_events(days=90, seed=None, now=None, tz_offset_hours=5, base_per_day=26):
    """Returns (list of webhook payloads sorted by time, ad_meta rows)."""
    rnd = random.Random(seed)
    now = int(now if now is not None else time.time())
    tz = timezone(timedelta(hours=tz_offset_hours))
    today = datetime.fromtimestamp(now, tz).date()
    start_day = today - timedelta(days=days - 1)
    ads = [dict(ad_id=a[0], headline=a[1], name=a[2], campaign=a[3], adset=a[4], media=a[5],
                platform=a[6], weight=a[7], first=a[8], last=a[9]) for a in CAMPAIGNS]
    u = UNNAMED_AD
    ads.append(dict(ad_id=u[0], headline=u[1], name="", campaign="", adset="", media=u[2],
                    platform=u[3], weight=u[4], first=u[5], last=u[6]))
    # each creative gets its own random "quality" so the ranking differs between runs
    for a in ads:
        a["weight"] *= rnd.uniform(0.7, 1.3)

    contacts = []            # (wa_id, name) of people who wrote before — some come back
    used = set()
    events = []
    counter = 0
    for d in range(days):
        day = start_day + timedelta(days=d)
        frac = d / max(days - 1, 1)
        trend = 0.8 + 0.4 * frac + 0.08 * math.sin(d / 9.0)
        expected = base_per_day * WEEKDAY[day.weekday()] * trend
        n = max(0, int(rnd.gauss(expected, expected * 0.15)))
        active = [a for a in ads if a["first"] <= frac <= a["last"]]
        ad_share = min(0.78, 0.45 + 0.04 * len(active)) * rnd.uniform(0.9, 1.1)
        for _ in range(n):
            hour = rnd.choices(range(24), weights=HOUR_WEIGHTS)[0]
            ts = int(datetime(day.year, day.month, day.day, hour, rnd.randint(0, 59), rnd.randint(0, 59),
                              tzinfo=tz).timestamp())
            if ts > now:
                continue
            counter += 1
            if contacts and rnd.random() < 0.06:
                wa_id, name = rnd.choice(contacts)
            else:
                wa_id, name = _phone(rnd, used), (rnd.choice(FIRST_NAMES) if rnd.random() > 0.12 else "")
                contacts.append((wa_id, name))
            ref = None
            if active and rnd.random() < ad_share:
                a = rnd.choices(active, weights=[x["weight"] for x in active])[0]
                url = (f"https://www.instagram.com/p/demo{a['ad_id'][-3:]}/" if a["platform"] == "instagram"
                       else f"https://fb.me/demo{a['ad_id'][-3:]}")
                ref = s.referral(a["ad_id"], a["headline"], source_url=url,
                                 ctwa_clid=None if rnd.random() < 0.1 else f"ARdemo{rnd.randint(10**8, 10**9)}")
                ref["media_type"] = a["media"]
            uid = f"{seed or 'r'}.{counter}"
            events.append((ts, s.webhook([s.message(f"wamid.demo.{uid}.0", wa_id, ts, rnd.choice(FIRST_TEXTS), ref)],
                                         name=name)))
            t = ts
            for k in range(rnd.choices([0, 1, 2, 3, 4, 5], weights=[25, 25, 20, 14, 10, 6])[0]):
                t += rnd.randint(40, 1800)
                if t > now:
                    break
                events.append((t, s.webhook([s.message(f"wamid.demo.{uid}.{k + 1}", wa_id, t,
                                                       rnd.choice(FOLLOW_UPS))], name=name)))
    events.sort(key=lambda e: e[0])
    meta = [(a["ad_id"], a["name"], a["adset"], a["campaign"]) for a in ads if a["name"]]
    return rnd, [p for _, p in events], meta


def seed(db, cfg, days=90, seed=None, now=None):
    """Fills an (empty) database with demo data. Returns the number of inquiries created."""
    now = int(now if now is not None else time.time())
    rnd, payloads, meta = generate_events(days, seed, now, cfg.tz_offset_hours)
    for payload in payloads:
        for m in extract_messages(payload, cfg.phone_number_id or s.NUMBER_7000):
            record_message(db, m, cfg.inquiry_gap_days, now=m.ts + rnd.randint(1, 5))
    with db.transaction() as c:
        for ad_id, name, adset, campaign in meta:
            c.execute("INSERT OR REPLACE INTO ad_meta (ad_id, ad_name, adset_name, campaign_name, fetched_at)"
                      " VALUES (?, ?, ?, ?, ?)", (ad_id, name, adset, campaign, now))
    seed_amo(db, cfg, rnd, now)
    return db.query("SELECT COUNT(*) AS n FROM inquiries")[0]["n"]


def seed_amo(db, cfg, rnd, now):
    """amoCRM column: final deal states only (as if the background sync already processed them)."""
    rows = db.query(
        "SELECT i.id, i.source, i.wa_id, i.started_at, i.headline, COALESCE(m.ad_name, '') AS ad_name"
        " FROM inquiries i LEFT JOIN ad_meta m ON m.ad_id = i.ad_id AND i.ad_id != ''"
        " WHERE NOT EXISTS (SELECT 1 FROM amo_leads a WHERE a.inquiry_id = i.id)")
    with db.transaction() as c:
        for r in rows:
            if rnd.random() < 0.15:
                continue  # some inquiries never become a deal
            before = rnd.choice(["", "", "", "Органика WhatsApp", "Органика Instagram", "Сарафанное радио"])
            label = r["ad_name"] or r["headline"]
            if r["source"] == "ad":
                state = rnd.choices(["written", "dry_run", "already"], weights=[70, 15, 15])[0]
                action = f"Таргет · {label}" if not before else f"было «{before}» → Таргет · {label}"
                if state == "already":
                    before, action = "Таргетированная реклама в Инстаграм или Фейсбук", f"уже стоял Таргет · {label}"
            else:
                state, action = "organic", ""
            c.execute(
                "INSERT OR IGNORE INTO amo_leads (lead_id, pipeline_id, created_at, received_at, phones,"
                " source_before, state, inquiry_id, action, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (31000000 + r["id"], cfg.amo_pipeline_id, r["started_at"] + 30, r["started_at"] + 31,
                 r["wa_id"], before, state, r["id"], action, r["started_at"] + 120))


def main():
    from config import load_config
    from db import Database

    ap = argparse.ArgumentParser(description="Generate random demo data into a SQLite file.")
    ap.add_argument("--db", default="data/demo.db")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config(overrides={"DEMO_MODE": "0", "DB_PATH": args.db})
    db = Database(cfg.db_path)
    if db.query("SELECT COUNT(*) AS n FROM inquiries")[0]["n"]:
        raise SystemExit(f"{cfg.db_path} is not empty — delete it first.")
    n = seed(db, cfg, days=args.days, seed=args.seed)
    db.close()
    print(f"{cfg.db_path}: {n} demo inquiries over {args.days} days")


if __name__ == "__main__":
    main()
