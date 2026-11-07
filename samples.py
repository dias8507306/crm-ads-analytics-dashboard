"""Тела вебхуков WhatsApp в том виде, в каком их шлёт Meta. Нужны тестам и tools/simulate.py."""
import hashlib
import hmac
import json

NUMBER_7000 = "100000000007000"
NUMBER_2000 = "100000000002000"


def message(wamid, wa_id, ts, text="Здравствуйте!", referral=None, kind="text"):
    msg = {"from": wa_id, "id": wamid, "timestamp": str(ts), "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    elif kind == "reaction":
        msg["reaction"] = {"message_id": "wamid.X", "emoji": "👍"}
    else:
        msg[kind] = {"id": "media-1", "caption": text}
    if referral:
        msg["referral"] = referral
    return msg


def referral(ad_id, headline, source_url="https://fb.me/ad", ctwa_clid="ARclid", source_type="ad", body=""):
    ref = {"source_url": source_url, "source_id": ad_id, "source_type": source_type,
           "headline": headline, "body": body, "media_type": "video",
           "thumbnail_url": "https://scontent.example/thumb.jpg"}
    if ctwa_clid is not None:
        ref["ctwa_clid"] = ctwa_clid
    return ref


def webhook(messages, phone_number_id=NUMBER_7000, name="Клиент", waba_id="WABA_7000"):
    contacts = [{"profile": {"name": name}, "wa_id": m["from"]} for m in messages]
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": waba_id,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "77000007000", "phone_number_id": phone_number_id},
                    "contacts": contacts,
                    "messages": messages,
                },
            }],
        }],
    }


def status_update(phone_number_id=NUMBER_7000):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA_7000", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "77000007000", "phone_number_id": phone_number_id},
            "statuses": [{"id": "wamid.OUT1", "status": "delivered", "timestamp": "1760000000",
                          "recipient_id": "77000000000"}]}}]}]}


def encode(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def sign(app_secret, body):
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
