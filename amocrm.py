"""amoCRM: у сделок с 7000, пришедших с рекламы, ставим «Қайдан келді» = Таргет.

Как работает:
  1. amoCRM присылает вебхук «сделка добавлена» → POST /amo/webhook?key=...
     Сделки из воронки «ОП» (туда Wazzup кладёт 7000) ставим в очередь — таблица amo_leads.
  2. Через минуту: GET сделки с контактом, GET контакта (телефоны кэшируются в amo_contacts).
  3. Ищем наше обращение по телефону. Если оно с рекламы — PATCH сделки:
       «Қайдан келді» = «Таргетированная реклама в Инстаграм или Фейсбук», даже если менеджер
       выбрал другое (решение 2026-10-12: данные Meta точнее);
       utm_source / utm_medium / utm_campaign / utm_content — откуда, кампания и креатив
       (видно на вкладке «Статистика» в карточке).
     Органику не трогаем: «Органика WhatsApp», «Органика Instagram» и сарафан мы не различаем.

Ограничения amoCRM (за превышение лимитов аккаунт блокируют):
  * не чаще 2 запросов в секунду (лимит компании — 3 суммарно) и не больше 1000 в час;
  * один и тот же запрос не повторяем раньше чем через 15 минут, и не больше 3 раз;
    одна сделка — один GET и не больше одного PATCH, контакт читаем один раз;
  * 429 — пауза 2 минуты; 401/403 — стоп на час и громкое сообщение в логе.
Режимы AMO_MODE: off — ничего не делаем; dry-run — читаем и пишем в журнал, что поставили бы;
on — пишем в amoCRM. Токен в логи и ошибки не попадает.
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs

from attribution import creative_label
from logutil import log

MIN_INTERVAL = 0.5                 # секунд между запросами → не больше 2 в секунду
HOURLY_CAP = 1000
REPEAT_GUARD_SECONDS = 15 * 60     # тот же запрос раньше — нельзя
MAX_ATTEMPTS = 3
PROCESS_DELAY = 60                 # Wazzup создаёт сделку одновременно с сообщением — даём вебхуку Meta дойти
MATCH_WAIT = 30 * 60               # сколько ждать наше обращение, если вебхук Meta запаздывает
MATCH_RETRY = 120
LEAD_WINDOW_BEFORE = 3600          # обращение должно быть активно не раньше чем за час до сделки
LEAD_WINDOW_AFTER = 15 * 60
UTM_MEDIUM = "ctwa"                # click-to-WhatsApp


class AmoError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class AmoClient:
    """Все запросы к amoCRM — только через этот клиент: лимитер, защита от повторов, стоп при блокировке."""

    def __init__(self, domain, token, transport=None, clock=time.time, sleep=time.sleep):
        self.base = f"https://{domain}"
        self.token = token
        self._transport = transport or self._urllib
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0
        self._recent = {}              # ключ запроса → (время последнего, сколько раз)
        self._hour = []                # времена запросов за последний час
        self.paused_until = 0.0
        self.stopped_reason = ""

    def _urllib(self, method, url, body):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def _scrub(self, text):
        return text.replace(self.token, "***") if self.token else text

    def request(self, method, path, body=None):
        key = method + " " + path + " " + hashlib.sha1(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            now = self._clock()
            if self.stopped_reason and now < self.paused_until:
                raise AmoError(0, "amoCRM на паузе: " + self.stopped_reason)
            if now < self.paused_until:
                raise AmoError(0, "amoCRM на паузе после 429")
            last, count = self._recent.get(key, (0.0, 0))
            if count and (now - last < REPEAT_GUARD_SECONDS or count >= MAX_ATTEMPTS):
                raise AmoError(0, "повтор того же запроса не разрешён")
            self._hour = [t for t in self._hour if now - t < 3600]
            if len(self._hour) >= HOURLY_CAP:
                raise AmoError(0, "достигнут часовой лимит запросов")
            wait = MIN_INTERVAL - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._last = now
            self._hour.append(now)
            self._recent[key] = (now, count + 1)
            if len(self._recent) > 5000:
                self._recent = {k: v for k, v in self._recent.items() if now - v[0] < 86400}
            try:
                status, data = self._transport(method, self.base + path, body)
            except Exception as e:  # сеть
                raise AmoError(0, self._scrub(f"{type(e).__name__}: {e}")[:300]) from None

            if status == 429:
                self.paused_until = now + 120
            elif status in (401, 403):
                self.stopped_reason = f"amoCRM ответила {status} — токен отозван или доступ заблокирован"
                self.paused_until = now + 3600
                log(f"[amo] !!! {self.stopped_reason}. Запросы остановлены на час.")
            if status >= 400:
                detail = data.get("title") or data.get("detail") or data.get("raw") or ""
                if data.get("validation-errors"):
                    detail += " " + json.dumps(data["validation-errors"], ensure_ascii=False)[:300]
                raise AmoError(status, self._scrub(str(detail))[:400])
            return data


def normalize_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def contact_phones(contact):
    phones = []
    for f in contact.get("custom_fields_values") or []:
        if f.get("field_code") == "PHONE":
            for v in f.get("values") or []:
                p = normalize_phone(v.get("value"))
                if len(p) >= 10 and p not in phones:
                    phones.append(p)
    return phones


def parse_webhook(body):
    """Тело вебхука amoCRM (x-www-form-urlencoded) → список новых сделок."""
    form = parse_qs(body.decode("utf-8", "replace"))
    leads = {}
    for key, values in form.items():
        m = re.fullmatch(r"leads\[add\]\[(\d+)\]\[(\w+)\]", key)
        if m:
            leads.setdefault(m.group(1), {})[m.group(2)] = values[0]
    account_id = (form.get("account[id]") or [""])[0]
    return account_id, [l for l in leads.values() if str(l.get("id", "")).isdigit()]


def utm_source_of(source_url):
    url = (source_url or "").lower()
    if "instagram" in url:
        return "instagram"
    if "facebook" in url or "fb.me" in url or "fb.com" in url:
        return "facebook"
    return "meta"


class AmoSync:
    def __init__(self, db, cfg, client=None, clock=time.time):
        self.db = db
        self.cfg = cfg
        self.client = client or AmoClient(cfg.amo_domain, cfg.amo_token)
        self._clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()

    @property
    def enabled(self):
        return self.cfg.amo_mode in ("dry-run", "on") and bool(self.cfg.amo_token)

    # --- вебхук ---

    def accept_webhook(self, body):
        account_id, leads = parse_webhook(body)
        if account_id and account_id != str(self.cfg.amo_account_id):
            return 0
        now = int(self._clock())
        added = 0
        for lead in leads:
            pipeline = int(lead.get("pipeline_id") or 0)
            if pipeline and pipeline != self.cfg.amo_pipeline_id:
                continue
            with self.db.transaction() as c:
                cur = c.execute(
                    "INSERT OR IGNORE INTO amo_leads (lead_id, pipeline_id, created_at, received_at,"
                    " next_try_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(lead["id"]), pipeline, int(lead.get("created_at") or now), now,
                     now + PROCESS_DELAY, now))
                added += cur.rowcount
        if added:
            self._wake.set()
        return added

    # --- обработка очереди ---

    def _update(self, lead_id, **fields):
        fields["updated_at"] = int(self._clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE amo_leads SET {cols} WHERE lead_id = ?", (*fields.values(), lead_id))

    def due(self, limit=20):
        return self.db.query(
            "SELECT * FROM amo_leads WHERE state IN ('new', 'fetched') AND next_try_at <= ?"
            " ORDER BY next_try_at LIMIT ?", (int(self._clock()), limit))

    def run_once(self):
        if not self.enabled:
            return 0
        done = 0
        for row in self.due():
            if self._stop.is_set():
                break
            try:
                self.process(row)
            except AmoError as e:
                self._failed(row, e)
            done += 1
        return done

    def _failed(self, row, err):
        attempts = row["attempts"] + 1
        if err.status == 404:
            self._update(row["lead_id"], state="gone", last_error=str(err), attempts=attempts)
        elif err.status and 400 <= err.status < 500 and err.status != 429 or attempts >= MAX_ATTEMPTS:
            self._update(row["lead_id"], state="error", last_error=str(err), attempts=attempts)
        else:  # 429, 5xx, сеть, пауза — попробуем позже, но не раньше защиты от повторов
            self._update(row["lead_id"], last_error=str(err), attempts=attempts,
                         next_try_at=int(self._clock()) + REPEAT_GUARD_SECONDS + 60)

    def process(self, row):
        lead_id = row["lead_id"]
        if row["state"] == "new":
            row = self._fetch(row)
            if row["state"] != "fetched":
                return
        self._match_and_write(row)

    def _fetch(self, row):
        lead_id = row["lead_id"]
        lead = self.client.request("GET", f"/api/v4/leads/{lead_id}?with=contacts")
        pipeline = int(lead.get("pipeline_id") or 0)
        before, before_enum = "", 0
        for f in lead.get("custom_fields_values") or []:
            if f.get("field_id") == self.cfg.amo_source_field_id and f.get("values"):
                before = str(f["values"][0].get("value") or "")
                before_enum = int(f["values"][0].get("enum_id") or 0)
        contacts = (lead.get("_embedded") or {}).get("contacts") or []
        main = next((c for c in contacts if c.get("is_main")), contacts[0] if contacts else None)
        fields = dict(pipeline_id=pipeline, created_at=int(lead.get("created_at") or row["created_at"]),
                      source_before=before, source_before_enum=before_enum)
        if pipeline != self.cfg.amo_pipeline_id:
            self._update(lead_id, state="skipped", **fields)
            return dict(row, state="skipped")
        if not main:
            self._update(lead_id, state="no_match", last_error="у сделки нет контакта", **fields)
            return dict(row, state="no_match")

        contact_id = int(main["id"])
        cached = self.db.query("SELECT phones FROM amo_contacts WHERE contact_id = ?", (contact_id,))
        if cached:
            phones = cached[0]["phones"]
        else:
            contact = self.client.request("GET", f"/api/v4/contacts/{contact_id}")
            phones = ",".join(contact_phones(contact))
            self.db.execute("INSERT OR REPLACE INTO amo_contacts (contact_id, phones, fetched_at)"
                            " VALUES (?, ?, ?)", (contact_id, phones, int(self._clock())))
        self._update(lead_id, state="fetched", contact_id=contact_id, phones=phones, **fields)
        return dict(row, state="fetched", contact_id=contact_id, phones=phones, **fields)

    def find_inquiry(self, phones, lead_created_at):
        phones = [p for p in (phones or "").split(",") if p]
        if not phones:
            return None
        marks = ",".join("?" * len(phones))
        rows = self.db.query(
            "SELECT i.*, COALESCE(m.ad_name, '') AS ad_name, COALESCE(m.campaign_name, '') AS campaign_name"
            " FROM inquiries i LEFT JOIN ad_meta m ON m.ad_id = i.ad_id AND i.ad_id != ''"
            f" WHERE i.wa_id IN ({marks}) AND i.started_at <= ? AND i.last_at >= ?"
            " ORDER BY i.started_at DESC, i.id DESC LIMIT 1",
            (*phones, lead_created_at + LEAD_WINDOW_AFTER, lead_created_at - LEAD_WINDOW_BEFORE))
        return rows[0] if rows else None

    def patch_body(self, inquiry, with_utm=True):
        fields = [{"field_id": self.cfg.amo_source_field_id,
                   "values": [{"enum_id": self.cfg.amo_target_enum_id}]}]
        if with_utm:
            utm = {
                "utm_source": utm_source_of(inquiry.get("source_url")),
                "utm_medium": UTM_MEDIUM,
                "utm_campaign": inquiry.get("campaign_name") or "",
                "utm_content": creative_label(inquiry),
            }
            for name, value in utm.items():
                field_id = self.cfg.amo_utm_fields.get(name)
                if field_id and value:
                    fields.append({"field_id": field_id, "values": [{"value": str(value)[:250]}]})
        return {"custom_fields_values": fields}

    def _match_and_write(self, row):
        lead_id = row["lead_id"]
        now = int(self._clock())
        inquiry = self.find_inquiry(row["phones"], row["created_at"])
        if inquiry is None:
            if now - row["received_at"] < MATCH_WAIT:
                self._update(lead_id, next_try_at=now + MATCH_RETRY)
            else:
                self._update(lead_id, state="no_match")
            return
        if inquiry["source"] != "ad":
            self._update(lead_id, state="organic", inquiry_id=inquiry["id"])
            return

        label = creative_label(inquiry)
        action = f"Таргет · {label}"
        if row["source_before"] and row["source_before_enum"] != self.cfg.amo_target_enum_id:
            action = f"было «{row['source_before']}» → {action}"
        if row["source_before_enum"] == self.cfg.amo_target_enum_id:
            self._update(lead_id, state="already", inquiry_id=inquiry["id"], action=f"уже стоял Таргет · {label}")
            return
        if self.cfg.amo_mode != "on":
            self._update(lead_id, state="dry_run", inquiry_id=inquiry["id"], action=action)
            return

        path = f"/api/v4/leads/{lead_id}"
        try:
            self.client.request("PATCH", path, self.patch_body(inquiry))
        except AmoError as e:
            if e.status != 400:
                raise
            # amoCRM не приняла utm-поля — ставим хотя бы «Қайдан келді»
            self.client.request("PATCH", path, self.patch_body(inquiry, with_utm=False))
            action += " (без utm)"
        self._update(lead_id, state="written", inquiry_id=inquiry["id"], action=action, last_error="")
        log(f"[amo] сделка {lead_id}: {action}")

    # --- фон ---

    def start(self, interval=15):
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:  # поток не должен умирать
                    log(f"[amo] ошибка: {type(e).__name__}")
                self._wake.wait(interval)
                self._wake.clear()

        t = threading.Thread(target=loop, name="amo-sync", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()
        self._wake.set()

    def summary(self):
        rows = self.db.query("SELECT state, COUNT(*) AS n FROM amo_leads GROUP BY state")
        return {r["state"]: r["n"] for r in rows}
