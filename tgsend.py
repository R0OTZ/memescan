"""Minimal Telegram Bot API client shared by the listener, watcher and scanner."""
import json
import os
import time
import urllib.error
import urllib.request


def state_dir():
    return os.path.dirname(os.environ.get("STATE_PATH", "state/state_v2.json")) or "."


def owner_chat():
    c = os.environ.get("TELEGRAM_CHAT_ID")
    if c:
        return c
    try:
        with open(os.path.join(state_dir(), "tg.json")) as f:
            return json.load(f).get("chat_id")
    except Exception:
        return None


def call(method, params, timeout=20):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    for i in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i == 0:
                time.sleep(3)
                continue
            print(f"telegram {method} failed: HTTP {e.code}", flush=True)
            return None
        except Exception as e:
            print(f"telegram {method} failed: {type(e).__name__}", flush=True)
            return None


def send(text, chat=None, buttons=None):
    chat = chat or owner_chat()
    if not chat:
        return None
    p = dict(chat_id=chat, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True)
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    return call("sendMessage", p)
