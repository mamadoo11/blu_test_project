import json
import os
import threading
import urllib.request
import urllib.error
from flask import Flask, request, jsonify

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OWNER_ID = os.environ.get("OWNER_ID", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free").strip()
app = Flask(__name__)
histories = {}
seen_updates = set()
updates_lock = threading.Lock()

SYSTEM_PROMPT = (
    "تو دستیار شخصی فارسی‌زبان کاربر هستی. واضح و کاربردی پاسخ بده. "
    "در این نسخه فقط راهنمایی و تولید متن/کد انجام می‌دهی؛ فرمانی روی رایانه اجرا "
    "نمی‌کنی و فایلی را تغییر نمی‌دهی. هرگز کلید محرمانه از کاربر نخواه."
)

def post_json(url, payload, headers=None, timeout=35):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def tg(method, payload):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return post_json(f"https://api.telegram.org/bot{TOKEN}/{method}", payload, timeout=15)

def ask_ai(text, history):
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": text[:6000]})
    result = post_json(
        "https://openrouter.ai/api/v1/chat/completions",
        {"model": MODEL, "messages": messages, "temperature": 0.4, "max_tokens": 800},
        {"Authorization": f"Bearer {API_KEY}",
         "HTTP-Referer": os.environ.get("RENDER_EXTERNAL_URL", "https://render.com"),
         "X-OpenRouter-Title": "OZAL Personal Agent"},
        timeout=35,
    )
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("مدل هوش مصنوعی پاسخ قابل استفاده‌ای برنگرداند")
    answer = (choices[0].get("message") or {}).get("content") or ""
    answer = answer.strip()
    if not answer:
        raise RuntimeError("پاسخ مدل خالی بود")
    return answer

def send(chat_id, text):
    while text:
        part = text[:3900]
        if len(text) > 3900:
            cut = part.rfind("\n")
            if cut > 1000:
                part = part[:cut]
        tg("sendMessage", {"chat_id": chat_id, "text": part})
        text = text[len(part):].lstrip()

def handle_update(update):
    update_id = update.get("update_id")
    if update_id is not None:
        with updates_lock:
            if update_id in seen_updates:
                return
            seen_updates.add(update_id)
            if len(seen_updates) > 3000:
                seen_updates.clear()
                seen_updates.add(update_id)
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (message.get("from") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return
    if OWNER_ID and str(user_id) != OWNER_ID:
        send(chat_id, "این ربات خصوصی است و فقط مالک اجازه استفاده دارد.")
        return
    if text == "/start":
        reply = "سلام! ایجنت شخصی OZAL آماده است. پیامت را بفرست. دستور /reset تاریخچهٔ گفت‌وگوی فعلی را پاک می‌کند."
    elif text == "/reset":
        histories.pop(chat_id, None)
        reply = "تاریخچهٔ گفت‌وگوی فعلی پاک شد."
    else:
        try:
            # Show Telegram's typing indicator while the model is working.
            tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            history = histories.setdefault(chat_id, [])
            reply = ask_ai(text, history)
            history.extend([{"role": "user", "content": text[:6000]},
                            {"role": "assistant", "content": reply}])
            histories[chat_id] = history[-8:]
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:180]
            reply = f"خطای سرویس هوش مصنوعی ({exc.code}). احتمالاً مدل رایگان محدود یا شلوغ است.\n{details}"
        except Exception as exc:
            app.logger.exception("AI request failed")
            reply = f"ارتباط با هوش مصنوعی ناموفق بود ({type(exc).__name__}). کمی بعد دوباره تلاش کن."
    try:
        send(chat_id, reply)
    except Exception:
        app.logger.exception("Telegram send failed")

@app.get("/")
def health():
    return jsonify(status="ok", service="OZAL Telegram Agent"), 200

@app.post("/telegram")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return "Forbidden", 403
    if not TOKEN or not API_KEY:
        return "Service not configured", 503
    update = request.get_json(silent=True) or {}
    # Acknowledge Telegram immediately; process AI requests outside the webhook
    # request so slow model responses do not make Telegram retry the update.
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return "ok", 200

def configure_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base or not TOKEN:
        return
    payload = {"url": base + "/telegram", "allowed_updates": ["message"]}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    try:
        result = tg("setWebhook", payload)
        app.logger.info("Telegram webhook configured: %s", result.get("ok"))
    except Exception:
        app.logger.exception("Webhook setup failed; it can be retried after deployment")

# Render supplies RENDER_EXTERNAL_URL at runtime.
if TOKEN and os.environ.get("RENDER_EXTERNAL_URL"):
    configure_webhook()
