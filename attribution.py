"""Разбор вебхука WhatsApp и правило «что считать обращением».

Обращение — это:
  * первое сообщение человека на 7000;
  * первое сообщение после тишины дольше INQUIRY_GAP_DAYS;
  * сообщение с referral (человек нажал рекламу), если текущее обращение не от этой же рекламы.
    Повторный клик по той же рекламе внутри открытого обращения новым не считается.

Таргет или органика решается только по referral в сообщении, которое открыло обращение.
referral без ctwa_clid — всё равно таргет.
"""
import time
from dataclasses import dataclass, field

FIRST_TEXT_MAX = 300  # в дашборде нужно только начало первого сообщения

# Это не сообщения от человека — обращение ими не открываем и не продлеваем.
IGNORED_TYPES = {"system", "reaction", "request_welcome"}

TYPE_LABELS = {
    "image": "[фото]", "video": "[видео]", "audio": "[аудио]", "voice": "[голосовое]",
    "document": "[файл]", "sticker": "[стикер]", "location": "[геолокация]",
    "contacts": "[контакт]", "unsupported": "[неподдерживаемое сообщение]",
}


@dataclass
class IncomingMessage:
    wamid: str
    wa_id: str
    ts: int
    type: str
    text: str = ""
    profile_name: str = ""
    referral: dict = field(default_factory=dict)


def _message_text(msg):
    kind = msg.get("type", "")
    if kind == "text":
        return (msg.get("text") or {}).get("body", "")
    if kind == "button":
        return (msg.get("button") or {}).get("text", "")
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    caption = (msg.get(kind) or {}).get("caption", "") if isinstance(msg.get(kind), dict) else ""
    label = TYPE_LABELS.get(kind, f"[{kind}]" if kind else "")
    return f"{label} {caption}".strip()


def extract_messages(payload, phone_number_id):
    """Входящие сообщения на нужный номер из тела вебхука. Статусы доставки и чужие номера — мимо."""
    found = []
    if not isinstance(payload, dict):
        return found
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if str((value.get("metadata") or {}).get("phone_number_id", "")) != str(phone_number_id):
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                     for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                kind = msg.get("type", "")
                if kind in IGNORED_TYPES or not msg.get("id") or not msg.get("from"):
                    continue
                try:
                    ts = int(msg.get("timestamp") or 0) or int(time.time())
                except (TypeError, ValueError):
                    ts = int(time.time())
                referral = msg.get("referral")
                found.append(IncomingMessage(
                    wamid=msg["id"],
                    wa_id=str(msg["from"]),
                    ts=ts,
                    type=kind,
                    text=_message_text(msg)[:FIRST_TEXT_MAX],
                    profile_name=names.get(msg["from"], ""),
                    referral=referral if isinstance(referral, dict) and referral else {},
                ))
    return found


def record_message(db, m, gap_days, now=None):
    """Пишет сообщение в базу. Возвращает 'duplicate' | 'new_ad' | 'new_organic' | 'appended'."""
    now = int(now if now is not None else time.time())
    gap = gap_days * 86400
    ref = m.referral
    ad_id = str(ref.get("source_id") or "") if ref else ""

    with db.transaction() as c:
        if c.execute("SELECT 1 FROM messages WHERE wamid = ?", (m.wamid,)).fetchone():
            return "duplicate"

        last = c.execute(
            "SELECT id, source, ad_id, last_at FROM inquiries WHERE wa_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1", (m.wa_id,)).fetchone()

        start_new = (
            last is None
            or m.ts - last["last_at"] >= gap
            or (bool(ref) and not (last["source"] == "ad" and last["ad_id"] == ad_id))
        )

        if start_new:
            cur = c.execute(
                "INSERT INTO inquiries (wa_id, profile_name, started_at, last_at, messages, first_wamid,"
                " first_text, source, ad_id, source_type, source_url, headline, body, media_type, ctwa_clid)"
                " VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m.wa_id, m.profile_name, m.ts, m.ts, m.wamid, m.text,
                 "ad" if ref else "organic", ad_id,
                 str(ref.get("source_type") or ""), str(ref.get("source_url") or ""),
                 str(ref.get("headline") or ""), "",  # текст объявления (body) не храним — он не нужен
                 str(ref.get("media_type") or ""),
                 str(ref.get("ctwa_clid") or "")))  # пустой ctwa_clid храним как '' — это нормально
            inquiry_id = cur.lastrowid
            result = "new_ad" if ref else "new_organic"
        else:
            inquiry_id = last["id"]
            c.execute(
                "UPDATE inquiries SET last_at = MAX(last_at, ?), messages = messages + 1,"
                " profile_name = CASE WHEN profile_name = '' THEN ? ELSE profile_name END"
                " WHERE id = ?", (m.ts, m.profile_name, inquiry_id))
            result = "appended"

        c.execute(
            "INSERT INTO messages (wamid, wa_id, ts, type, has_referral, inquiry_id, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (m.wamid, m.wa_id, m.ts, m.type, 1 if ref else 0, inquiry_id, now))
    return result


def creative_label(row):
    """Как показать креатив, пока (или если) имени из Meta нет."""
    if row.get("source") != "ad":
        return "Органика (без рекламы)"
    return (row.get("ad_name") or row.get("headline") or row.get("source_url")
            or (f"Объявление {row['ad_id']}" if row.get("ad_id") else "Реклама без данных"))


def creative_key(row):
    if row.get("source") != "ad":
        return "organic"
    if row.get("ad_id"):
        return "ad:" + row["ad_id"]
    return "ref:" + (row.get("headline") or "") + "|" + (row.get("source_url") or "")
