"""Настройки: переменные окружения важнее файла .env, .env важнее значений по умолчанию."""
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def read_env_file(path):
    values = {}
    p = Path(path)
    if not p.exists():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0]  # комментарий в конце строки
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Config:
    access_token: str
    app_secret: str
    verify_token: str
    graph_version: str
    phone_number_id: str        # 7000 = 100000000007000; сообщения на другие номера игнорируются
    dashboard_password: str
    session_secret: bytes
    session_ttl_days: int
    cookie_secure: bool
    db_path: str
    host: str
    port: int
    tz_offset_hours: int        # Казахстан — UTC+5
    inquiry_gap_days: int       # тишина дольше этого — следующее сообщение считается новым обращением
    ad_names_enabled: bool
    # --- amoCRM (your_amo_subdomain) ---
    amo_domain: str
    amo_token: str
    amo_mode: str               # off | dry-run (читаем и пишем в журнал) | on (пишем в amoCRM)
    amo_webhook_key: str        # секрет в адресе вебхука amoCRM: /amo/webhook?key=...
    amo_account_id: int
    amo_pipeline_id: int        # «ОП» — туда Wazzup кладёт сделки с 7000
    amo_source_field_id: int    # «Қайдан келді»
    amo_target_enum_id: int     # «Таргетированная реклама в Инстаграм или Фейсбук»
    amo_utm_fields: dict        # utm_source / utm_medium / utm_campaign / utm_content → id поля
    # --- сводка для дашборда EduApp Sales (только агрегаты, без номеров) ---
    summary_key: str = ""        # пусто — /api/summary выключен
    summary_origins: tuple = ()  # сайты, которым браузер разрешит читать сводку (CORS)
    # --- демо: база в памяти со случайными данными, без пароля, без запросов в Meta и amoCRM ---
    demo_mode: bool = False


def _amo_mode(value):
    value = value.strip().lower()
    return value if value in ("off", "dry-run", "on") else "off"


def load_config(env_file=None, overrides=None):
    file_values = read_env_file(env_file or BASE_DIR / ".env")

    def get(key, default=""):
        if overrides and key in overrides:
            return str(overrides[key])
        return os.environ.get(key, file_values.get(key, default))

    # DEMO_MODE=1 (по умолчанию): случайные данные в памяти, вход без пароля, внешние API выключены.
    # Для боевого запуска в .env должно быть DEMO_MODE=0.
    demo_mode = get("DEMO_MODE", "1") == "1"
    db_path = ":memory:" if demo_mode else get("DB_PATH", "data/leads.db")
    if db_path != ":memory:" and not Path(db_path).is_absolute():
        db_path = str(BASE_DIR / db_path)

    password = get("DASHBOARD_PASSWORD")
    app_secret = get("APP_SECRET")
    session_secret = get("SESSION_SECRET")
    # Без явного SESSION_SECRET ключ выводится из пароля: сменили пароль — старые входы сброшены.
    session_key = (session_secret.encode() if session_secret else
                   hmac.new(b"dashboard-7000-session", (password + "|" + app_secret).encode(),
                            hashlib.sha256).digest())

    return Config(
        access_token=get("ACCESS_TOKEN"),
        app_secret=app_secret,
        verify_token=get("VERIFY_TOKEN"),
        graph_version=get("GRAPH_VERSION", "v23.0"),
        phone_number_id=get("WA_PHONE_NUMBER_ID", "100000000007000"),
        dashboard_password=password,
        session_secret=session_key,
        session_ttl_days=int(get("SESSION_TTL_DAYS", "30")),
        cookie_secure=get("COOKIE_SECURE", "0") == "1",
        db_path=db_path,
        host=get("HOST", "127.0.0.1"),
        port=int(get("PORT", "8000")),
        tz_offset_hours=int(get("TZ_OFFSET_HOURS", "5")),
        inquiry_gap_days=int(get("INQUIRY_GAP_DAYS", "30")),
        ad_names_enabled=get("AD_NAMES_ENABLED", "1") == "1" and not demo_mode,
        amo_domain=get("AMO_DOMAIN", "YOUR_AMO_SUBDOMAIN.amocrm.ru"),
        amo_token=get("AMO_TOKEN"),
        amo_mode=_amo_mode(get("AMO_MODE", "dry-run")) if get("AMO_TOKEN") and not demo_mode else "off",
        amo_webhook_key=get("AMO_WEBHOOK_KEY"),
        amo_account_id=int(get("AMO_ACCOUNT_ID", "30000000")),
        amo_pipeline_id=int(get("AMO_PIPELINE_ID", "10000000")),
        amo_source_field_id=int(get("AMO_SOURCE_FIELD_ID", "1000001")),
        amo_target_enum_id=int(get("AMO_TARGET_ENUM_ID", "1000002")),
        amo_utm_fields={
            "utm_source": int(get("AMO_UTM_SOURCE_FIELD_ID", "1000011")),
            "utm_medium": int(get("AMO_UTM_MEDIUM_FIELD_ID", "1000012")),
            "utm_campaign": int(get("AMO_UTM_CAMPAIGN_FIELD_ID", "1000013")),
            "utm_content": int(get("AMO_UTM_CONTENT_FIELD_ID", "1000014")),
        },
        summary_key=get("SUMMARY_KEY"),
        summary_origins=tuple(o.strip().rstrip("/") for o in get("SUMMARY_ORIGINS", "https://eduapp-sales.example.com").split(",") if o.strip()),
        demo_mode=demo_mode,
    )
