#!/usr/bin/env python3
"""Instant Telegram replies for memescan.

Long-polls Telegram so commands get an answer within seconds, using the latest
snapshot that scanner.py writes after every scan (about once a minute).
Usage: python3 listener.py <seconds_to_run>
"""
import json
import os
import sys
import time
import urllib.request

STATE_DIR = os.path.dirname(os.environ.get("STATE_PATH", "state/state_v2.json")) or "."
TG_PATH = os.path.join(STATE_DIR, "tg.json")
SNAP_PATH = os.path.join(STATE_DIR, "snapshot.json")
HELP = "Commands: /summary /stats /best /copy /help"


def call(method, params, timeout=40):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"telegram {method} failed: {type(e).__name__}", flush=True)
        return None


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def handle(st, u, send):
    st["offset"] = max(st.get("offset", 0), u.get("update_id", 0))
    msg = u.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    owner = os.environ.get("TELEGRAM_CHAT_ID") or st.get("chat_id")
    if not owner and chat:
        st["chat_id"] = owner = str(chat)  # first chat to message the bot owns it
        send(owner, "🔒 Locked to this chat. " + HELP)
    if not chat or str(chat) != str(owner) or not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    key = {"/start": "/help", "/scan": "/best"}.get(cmd, cmd)
    if cmd == "/summary" and len(parts) > 1 and parts[1].isdigit():
        key = f"/summary#{int(parts[1])}"  # full breakdown of coin N
    snap = load(SNAP_PATH, {})
    replies = (snap.get("cmds") or {}).get(key)
    if replies is None:
        send(owner, ("Still warming up: first scan not finished yet. Try again in a minute."
                     if not snap else "Unknown command. " + HELP))
        return
    replies = replies or ["Nothing to show yet."]
    age = int(time.time() - snap.get("ts", time.time()))
    for i, t in enumerate(replies):
        send(owner, t + (f"\n\n<i>data {age}s old</i>" if i == len(replies) - 1 else ""))


def main(seconds):
    end = time.time() + seconds
    st = load(TG_PATH, {})
    if not st.get("chat_id"):  # carry over the chat/offset the scanner used before the listener existed
        old = load(os.path.join(STATE_DIR, "state_v2.json"), {})
        if old.get("chat_id"):
            st["chat_id"] = old["chat_id"]
        st.setdefault("offset", old.get("tg_offset", 0))
        save(TG_PATH, st)

    def send(chat, text):
        call("sendMessage", dict(chat_id=chat, text=text[:4000], parse_mode="HTML",
                                 disable_web_page_preview=True), timeout=15)

    while time.time() < end:
        wait = max(1, min(25, int(end - time.time())))
        r = call("getUpdates", {"offset": st.get("offset", 0) + 1, "timeout": wait}, timeout=wait + 10)
        for u in (r or {}).get("result") or []:
            handle(st, u, send)
        save(TG_PATH, st)
        if r is None:
            time.sleep(3)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3300)
