"""Названия объявлений из Meta: точечный GET /{ad_id}?fields=name,adset{name},campaign{name}.

Почему точечно, а не выгрузкой рекламного кабинета: реклама 7000 крутится не из того
кабинета, что был в старом .env, и кабинетов может стать больше. Запрос по ad_id из
referral не требует знать кабинет, тянет только объявления, которые реально привели людей,
и работает для любого кабинета, к которому у токена есть доступ (нужно право ads_read).
Пока имени нет — дашборд показывает креатив по headline / source_url из referral.

Токен уходит в заголовке Authorization, в URL и в логи не попадает.
"""
import json
import threading
import time
import urllib.error
import urllib.request

from logutil import log

MAX_ATTEMPTS = 5             # после стольких ошибок подряд объявление больше не спрашиваем
RETRY_BASE_SECONDS = 3600    # пауза перед повтором растёт: 1 ч, 2 ч, 3 ч...
REFRESH_AFTER_SECONDS = 7 * 86400   # имена переименованных объявлений обновляем раз в неделю
PAUSE_BETWEEN_REQUESTS = 0.5


class GraphError(Exception):
    pass


def _scrub(text, token):
    return text.replace(token, "***") if token else text


def graph_get(path, token, version, timeout=20):
    req = urllib.request.Request(f"https://graph.facebook.com/{version}/{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", "replace")).get("error", {})
            msg = f"HTTP {e.code}: {err.get('message', '')} (code {err.get('code')})"
        except ValueError:
            msg = f"HTTP {e.code}"
        raise GraphError(_scrub(msg, token)) from None
    except Exception as e:  # сеть, таймаут
        raise GraphError(_scrub(f"{type(e).__name__}: {e}", token)[:300]) from None


class AdNameFetcher:
    def __init__(self, db, config, fetch=None, sleep=time.sleep):
        self.db = db
        self.cfg = config
        self._fetch = fetch or (lambda ad_id: graph_get(
            f"{ad_id}?fields=name,adset{{name}},campaign{{name}}",
            config.access_token, config.graph_version))
        self._sleep = sleep
        self._wake = threading.Event()
        self._stop = threading.Event()

    @property
    def enabled(self):
        return bool(self.cfg.ad_names_enabled and self.cfg.access_token)

    def pending_ad_ids(self, now=None, limit=20):
        now = int(now if now is not None else time.time())
        rows = self.db.query(
            "SELECT i.ad_id, MAX(i.started_at) AS seen FROM inquiries i"
            " LEFT JOIN ad_meta m ON m.ad_id = i.ad_id"
            " WHERE i.ad_id != '' AND ("
            "   m.ad_id IS NULL"
            "   OR (m.ad_name = '' AND m.attempts < ? AND m.fetched_at <= ? - ? * m.attempts)"
            "   OR (m.ad_name != '' AND m.fetched_at <= ? - ?)"
            " ) GROUP BY i.ad_id ORDER BY seen DESC LIMIT ?",
            (MAX_ATTEMPTS, now, RETRY_BASE_SECONDS, now, REFRESH_AFTER_SECONDS, limit))
        # Настоящие id объявлений — только цифры; демо-данные и мусор в Meta не отправляем.
        return [r["ad_id"] for r in rows if r["ad_id"].isdigit()]

    def fetch_one(self, ad_id, now=None):
        now = int(now if now is not None else time.time())
        try:
            data = self._fetch(ad_id)
        except GraphError as e:
            self.db.execute(
                "INSERT INTO ad_meta (ad_id, fetched_at, attempts, last_error) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(ad_id) DO UPDATE SET fetched_at = excluded.fetched_at,"
                " attempts = ad_meta.attempts + 1, last_error = excluded.last_error",
                (ad_id, now, str(e)[:300]))
            return False
        self.db.execute(
            "INSERT INTO ad_meta (ad_id, ad_name, adset_name, campaign_name, fetched_at, attempts, last_error)"
            " VALUES (?, ?, ?, ?, ?, 0, '')"
            " ON CONFLICT(ad_id) DO UPDATE SET ad_name = excluded.ad_name, adset_name = excluded.adset_name,"
            " campaign_name = excluded.campaign_name, fetched_at = excluded.fetched_at,"
            " attempts = 0, last_error = ''",
            (ad_id, str(data.get("name") or ""), str((data.get("adset") or {}).get("name") or ""),
             str((data.get("campaign") or {}).get("name") or ""), now))
        return True

    def run_once(self):
        if not self.enabled:
            return 0
        done = 0
        for ad_id in self.pending_ad_ids():
            if self._stop.is_set():
                break
            if self.fetch_one(ad_id):
                done += 1
            self._sleep(PAUSE_BETWEEN_REQUESTS)
        return done

    def wake(self):
        """Новое рекламное обращение — подтянуть имя сразу, не дожидаясь планового прохода."""
        self._wake.set()

    def start(self, interval=300):
        def loop():
            while not self._stop.is_set():
                try:
                    n = self.run_once()
                    if n:
                        log(f"[ads] названия объявлений обновлены: {n}")
                except Exception as e:  # поток не должен умирать
                    log(f"[ads] ошибка: {type(e).__name__}")
                self._wake.wait(interval)
                self._wake.clear()

        t = threading.Thread(target=loop, name="ad-names", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()
        self._wake.set()
