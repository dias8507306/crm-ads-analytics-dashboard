"""Демо: шлёт на локальный сервер подписанные вебхуки, как будто люди пишут на 7000.

    python tools/simulate.py            # ~60 обращений за последние 14 дней
    python tools/simulate.py --count 10

Креативы выдуманные, их id начинаются с demo- — в Meta за названиями они не уходят.
Чтобы стереть демо: остановить сервер и удалить data/leads.db.
"""
import argparse
import http.client
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samples as s  # noqa: E402
from config import load_config  # noqa: E402

CREATIVES = [
    ("demo-ad-1", "Сторис: онлайн-мектеп 3–9 сынып", 9),
    ("demo-ad-2", "Сторис: ҰБТ-ға дайындық", 6),
    ("demo-ad-3", "Сторис: ағылшын тілі, тегін сабақ", 4),
    ("demo-ad-4", "Сторис: видео с учителем", 2),
]
ORGANIC_WEIGHT = 8
NAMES = ["Айгерим", "Ерлан", "Динара", "Нурлан", "Асель", "Мадина", "Арман", "Жанар", "Бауыржан",
         "Сауле", "Данияр", "Камила", "Ержан", "Гульнара", "", "Айдана", "Тимур", "Алия"]
FIRST_TEXTS = ["Здравствуйте! Хочу узнать подробнее", "Сәлеметсіз бе! Толығырақ айтып беріңізші",
               "Сколько стоит обучение?", "Добрый день, есть места на курс?", "Сәлем, бағасы қанша?"]
FOLLOW_UPS = ["А какое расписание?", "Ребёнку 12 лет", "Рахмет!", "Можно пробный урок?", "Хорошо, жду"]


def post(port, secret, payload):
    body = s.encode(payload)
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = s.sign(secret, body)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/webhook", body=body, headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    cfg = load_config()
    if cfg.demo_mode:
        sys.exit("simulate.py шлёт вебхуки на сервер с настоящей базой: запустите его с DEMO_MODE=0 "
                 "(в DEMO_MODE данные и так генерируются при старте — см. demo_data.py).")
    rnd = random.Random()
    now = int(time.time())
    choices = [(ad, head) for ad, head, w in CREATIVES for _ in range(w)] + [None] * ORGANIC_WEIGHT
    sent = 0
    for i in range(args.count):
        wa_id = "7701" + "".join(rnd.choice("0123456789") for _ in range(7))
        ts = now - rnd.randint(0, args.days * 86400 - 3600)
        pick = rnd.choice(choices)
        ref = None
        if pick:
            ad_id, headline = pick
            ref = s.referral(ad_id, headline, source_url=f"https://www.instagram.com/stories/eduapp_demo/{ad_id}",
                             ctwa_clid=None if rnd.random() < 0.15 else f"ARclid{rnd.randint(10**6, 10**7)}")
        uid = f"{now}.{i}"
        first = s.message(f"wamid.demo.{uid}.0", wa_id, ts, rnd.choice(FIRST_TEXTS), ref)
        name = rnd.choice(NAMES)
        status = post(cfg.port, cfg.app_secret, s.webhook([first], name=name))
        if status != 200:
            sys.exit(f"Сервер ответил {status}. Он запущен (python app.py)? APP_SECRET совпадает?")
        sent += 1
        for k in range(rnd.randint(0, 4)):
            follow = s.message(f"wamid.demo.{uid}.{k + 1}", wa_id, ts + 60 * (k + 1), rnd.choice(FOLLOW_UPS))
            post(cfg.port, cfg.app_secret, s.webhook([follow], name=name))
        if i == 0:
            post(cfg.port, cfg.app_secret, s.webhook([first], name=name))  # повторная доставка — не посчитается
            post(cfg.port, cfg.app_secret, s.webhook(
                [s.message(f"wamid.demo.{uid}.2000", "77000009000", ts)], phone_number_id=s.NUMBER_2000))
    print(f"Отправлено {sent} демо-обращений (+ повтор и сообщение на 2000 — оба должны игнорироваться).")
    print(f"Демо-сделок amoCRM: {demo_amo(cfg, rnd, now)} (в настоящую amoCRM ничего не уходит).")
    print(f"Открой http://127.0.0.1:{cfg.port}/")


def demo_amo(cfg, rnd, now):
    """Колонка «amoCRM» в дашборде: пишем в базу готовые итоги, как будто сделки уже обработаны.
    Только конечные состояния — фоновая синхронизация такие строки не трогает и в amoCRM не ходит."""
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    rows = conn.execute(
        "SELECT i.id, i.source, i.wa_id, i.started_at, i.headline FROM inquiries i"
        " WHERE i.first_wamid LIKE 'wamid.demo.%'"
        " AND NOT EXISTS (SELECT 1 FROM amo_leads a WHERE a.inquiry_id = i.id)").fetchall()
    added = 0
    for inquiry_id, source, wa_id, started_at, headline in rows:
        if rnd.random() < 0.15:
            continue  # у части обращений сделки ещё нет
        before = rnd.choice(["", "", "Органика WhatsApp", "Органика Instagram"])
        if source == "ad":
            state = "already" if rnd.random() < 0.2 else "dry_run"
            action = f"Таргет · {headline}" if not before else f"было «{before}» → Таргет · {headline}"
        else:
            state, action = "organic", ""
        conn.execute(
            "INSERT OR IGNORE INTO amo_leads (lead_id, pipeline_id, created_at, received_at, phones,"
            " source_before, state, inquiry_id, action, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (99000000 + inquiry_id, cfg.amo_pipeline_id, started_at, now, wa_id,
             before if state != "already" else "Таргетированная реклама в Инстаграм или Фейсбук",
             state, inquiry_id, action, now))
        added += 1
    conn.commit()
    conn.close()
    return added


if __name__ == "__main__":
    main()
