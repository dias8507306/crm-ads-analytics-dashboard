"""Отчёт для дашборда: обращения за период и сводка «креатив → обращений». Только чтение."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from attribution import creative_key, creative_label

MAX_ROWS = 5000

# Где человек увидел рекламу — по домену ссылки из referral. Сторис/лента/reels Meta
# по отдельному обращению не сообщает: ссылка instagram.com/p/… одна для всех плейсментов.
PLATFORMS = (("instagram.com", "Instagram"), ("fb.me", "Facebook"), ("facebook.com", "Facebook"),
             ("fb.com", "Facebook"), ("wa.me", "WhatsApp"), ("whatsapp.com", "WhatsApp"))
MEDIA = {"image": "фото", "video": "видео"}


def platform_of(url):
    host = urlsplit(url or "").netloc.lower()
    for domain, name in PLATFORMS:
        if host == domain or host.endswith("." + domain):
            return name
    return "Meta" if url else ""


def period_bounds(date_from, date_to, tz_offset_hours):
    """Даты по местному времени (включительно) → [начало, конец) в unix-секундах."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    d1 = date.fromisoformat(date_from)
    d2 = date.fromisoformat(date_to)
    if d2 < d1:
        d1, d2 = d2, d1
    start = datetime(d1.year, d1.month, d1.day, tzinfo=tz)
    end = datetime(d2.year, d2.month, d2.day, tzinfo=tz) + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), d1.isoformat(), d2.isoformat()


def build_report(db, date_from, date_to, tz_offset_hours, source="all", creative=""):
    start, end, d1, d2 = period_bounds(date_from, date_to, tz_offset_hours)
    tz = timezone(timedelta(hours=tz_offset_hours))
    rows = db.query(
        "SELECT i.id, i.wa_id, i.profile_name, i.started_at, i.last_at, i.messages, i.first_text,"
        " i.source, i.ad_id, i.source_type, i.source_url, i.headline, i.body, i.media_type, i.ctwa_clid,"
        " COALESCE(m.ad_name, '') AS ad_name, COALESCE(m.adset_name, '') AS adset_name,"
        " COALESCE(m.campaign_name, '') AS campaign_name,"
        " COALESCE(a.lead_id, 0) AS amo_lead_id, COALESCE(a.state, '') AS amo_state,"
        " COALESCE(a.action, '') AS amo_action, COALESCE(a.source_before, '') AS amo_source_before"
        " FROM inquiries i LEFT JOIN ad_meta m ON m.ad_id = i.ad_id AND i.ad_id != ''"
        " LEFT JOIN amo_leads a ON a.lead_id = (SELECT MAX(lead_id) FROM amo_leads WHERE inquiry_id = i.id)"
        " WHERE i.started_at >= ? AND i.started_at < ?"
        " ORDER BY i.started_at DESC, i.id DESC", (start, end))

    creatives = {}
    totals = {"all": 0, "ad": 0, "organic": 0}
    for r in rows:
        r["creative_key"] = creative_key(r)
        r["creative"] = creative_label(r)
        r["started_local"] = datetime.fromtimestamp(r["started_at"], tz).strftime("%Y-%m-%d %H:%M")
        r["platform"] = platform_of(r["source_url"]) if r["source"] == "ad" else ""
        r["media"] = MEDIA.get(r["media_type"], r["media_type"]) if r["source"] == "ad" else ""
        totals["all"] += 1
        totals[r["source"]] += 1
        c = creatives.get(r["creative_key"])
        if c is None:
            c = creatives[r["creative_key"]] = {
                "key": r["creative_key"], "label": r["creative"], "source": r["source"],
                "ad_id": r["ad_id"], "ad_name": r["ad_name"], "adset_name": r["adset_name"],
                "campaign_name": r["campaign_name"], "headline": r["headline"],
                "source_url": r["source_url"], "count": 0, "last_at": r["started_local"],
                "platforms": {},
            }
        c["count"] += 1
        if r["platform"]:
            c["platforms"][r["platform"]] = c["platforms"].get(r["platform"], 0) + 1

    # Рекламные — по убыванию, органика отдельной строкой в конце.
    summary = sorted((c for c in creatives.values() if c["source"] == "ad"),
                     key=lambda c: (-c["count"], c["label"]))
    summary += [c for c in creatives.values() if c["source"] != "ad"]

    listed = rows
    if source in ("ad", "organic"):
        listed = [r for r in listed if r["source"] == source]
    if creative:
        listed = [r for r in listed if r["creative_key"] == creative]

    return {
        "from": d1, "to": d2, "tz_offset_hours": tz_offset_hours,
        "totals": totals,
        "creatives": summary,
        "inquiries": listed[:MAX_ROWS],
        "inquiries_total": len(listed),
        "truncated": len(listed) > MAX_ROWS,
    }



def build_summary(db, date_from, date_to, tz_offset_hours, top=50):
    """Сводка для дашборда EduApp Sales: только числа и названия рекламы — без номеров, имён и текстов клиентов."""
    start, end, d1, d2 = period_bounds(date_from, date_to, tz_offset_hours)
    tz = timezone(timedelta(hours=tz_offset_hours))
    rows = db.query(
        "SELECT i.started_at, i.source, i.ad_id, i.source_type, i.source_url, i.headline, i.media_type,"
        " COALESCE(m.ad_name, '') AS ad_name, COALESCE(m.adset_name, '') AS adset_name,"
        " COALESCE(m.campaign_name, '') AS campaign_name"
        " FROM inquiries i LEFT JOIN ad_meta m ON m.ad_id = i.ad_id AND i.ad_id != ''"
        " WHERE i.started_at >= ? AND i.started_at < ?", (start, end))
    totals = {"all": 0, "ad": 0, "organic": 0}
    days, creatives, platforms, media = {}, {}, {}, {}
    for r in rows:
        src = "ad" if r["source"] == "ad" else "organic"
        day = datetime.fromtimestamp(r["started_at"], tz).date().isoformat()
        totals["all"] += 1
        totals[src] += 1
        e = days.setdefault(day, {"date": day, "ad": 0, "organic": 0})
        e[src] += 1
        if src != "ad":
            continue
        pl = platform_of(r["source_url"]) or "Meta"
        md = MEDIA.get(r["media_type"], r["media_type"] or "")
        platforms[pl] = platforms.get(pl, 0) + 1
        if md:
            media[md] = media.get(md, 0) + 1
        key = creative_key(r)
        c = creatives.get(key)
        if c is None:
            c = creatives[key] = {"label": creative_label(r), "campaign": r["campaign_name"], "adset": r["adset_name"],
                                  "count": 0, "platforms": {}, "media": {}}
        c["count"] += 1
        c["platforms"][pl] = c["platforms"].get(pl, 0) + 1
        if md:
            c["media"][md] = c["media"].get(md, 0) + 1
    listed = sorted(creatives.values(), key=lambda c: (-c["count"], c["label"]))
    return {"from": d1, "to": d2, "tz_offset_hours": tz_offset_hours, "totals": totals,
            "days": [days[k] for k in sorted(days)], "creatives": listed[:top], "creatives_total": len(listed),
            "platforms": platforms, "media": media}


CSV_COLUMNS = [
    ("started_local", "Дата и время"), ("wa_id", "Номер клиента"), ("profile_name", "Имя в WhatsApp"),
    ("source", "Откуда"), ("platform", "Площадка"), ("media", "Формат"),
    ("creative", "Креатив"), ("campaign_name", "Кампания"),
    ("adset_name", "Группа объявлений"), ("ad_id", "ID объявления"), ("headline", "Заголовок рекламы"),
    ("source_url", "Ссылка на рекламу"), ("first_text", "Первое сообщение"), ("messages", "Сообщений"),
    ("amo_lead_id", "Сделка amoCRM"), ("amo_source_before", "Было в «Қайдан келді»"),
    ("amo_action", "Что поставили в amoCRM"),
]


def report_csv(report):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow([title for _, title in CSV_COLUMNS])
    for r in report["inquiries"]:
        row = dict(r, source="Таргет" if r["source"] == "ad" else "Органика",
                   amo_lead_id=r.get("amo_lead_id") or "")
        w.writerow([_safe_cell(row.get(k, "")) for k, _ in CSV_COLUMNS])
    return "﻿" + out.getvalue()  # BOM — чтобы Excel открыл кириллицу


def _safe_cell(value):
    """Текст клиента не должен превратиться в формулу при открытии в Excel."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not s.lstrip("+").isdigit() else s
