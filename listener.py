#!/usr/bin/env python3
"""Instant Telegram replies for memescan.

Long-polls Telegram so commands get an answer within seconds. Two kinds of commands:
  * snapshot commands (/summary, /stats, /positions...) answered from the latest scan (about once a minute);
  * live commands (/best, /token, /rugcheck, /watch..., /notifications, /copywatch, /copytrade) computed on the
    spot from fresh data.
Command replies only ever go to the chat that asked. They never switch on, trigger or count toward
background notifications.
Usage: python3 listener.py <seconds_to_run>
"""
import json
import os
import sys
import time

import tgsend

STATE_DIR = tgsend.state_dir()
TG_PATH = os.path.join(STATE_DIR, "tg.json")
SNAP_PATH = os.path.join(STATE_DIR, "snapshot.json")
HELP = ("<b>MemeScan commands</b>\n"
        "/best: strongest current opportunities (live, evidence-scored)\n"
        "/token &lt;address&gt;: full research report on one token\n"
        "/rugcheck &lt;address&gt;: rug-pull risk analysis\n"
        "/watch &lt;address&gt;: monitor a token every 10s · /unwatch · /watchlist · /watchstatus · /watchpause · /watchresume\n"
        "/summary: trending intelligence · /stats: your portfolio\n"
        "/copywatch: traders you track (alerts only) · /copytrade: copy-trade panel (simulated)\n"
        "/notifications on|off|status|settings: background alerts\n"
        "/positions · /calls (how past calls did)\n"
        "<i>Commands answer here only and never turn on background alerts.</i>")
LIVE = ("/best", "/token", "/rugcheck", "/watch", "/unwatch", "/watchlist", "/watchstatus", "/watchpause",
        "/watchresume", "/notifications", "/copywatch", "/copytrade", "/help", "/start")


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


def live_best(send, owner):
    """Re-price the scanner's latest shortlist with fresh market data so /best is never stale."""
    import discovery as D
    import providers as P
    import token_intel as T
    meta = load(os.path.join(STATE_DIR, "best.json"), {})
    if not meta.get("mints") and not meta.get("t"):
        send(owner, "First /best pass hasn't finished yet (runs every 2 minutes). Try again shortly.")
        return
    age = time.time() - meta.get("t", 0)
    cache = P.Cache(os.path.join(STATE_DIR, "pcache_best.json"))  # read-only here: never saved by the listener
    hist = T.History(os.path.join(STATE_DIR, "token_hist.json"))
    weights = T.load_weights(os.path.join(STATE_DIR, "weights.json"))
    fresh = D.refresh(meta.get("mints", []), cache, hist, weights, meta.get("tags")) if meta.get("mints") else []
    shown = [a for a in fresh if not a["disqualified"] and a["score"] >= D.CFG["min_score"]
             and a["verdict"] != T.VERDICTS[2]]
    shown.sort(key=lambda a: -a["score"])
    dropped = len(fresh) - len(shown)
    res = dict(shown=shown, ranked=fresh, rejected=dict(meta.get("rejected", {})),
               n=meta.get("n", 0), src={k: v for k, v in (meta.get("src") or {}).items()}, t=time.time())
    if dropped:
        res["rejected"][f"dropped out on the live re-check just now"] = dropped
    msgs = D.fmt(res)
    note = f"\n<i>Candidate scan {T.ago(age)} ago; prices and security re-checked now.</i>"
    if age > 600:
        note += "\n⚠️ The background scan is behind; candidates may be missing newer coins."
    msgs[0] += note
    for t in msgs:
        send(owner, t)


def live(cmd, args, send, owner, button_send):
    """Commands computed on demand."""
    if cmd in ("/help", "/start"):
        return send(owner, HELP)
    if cmd == "/best":
        return live_best(send, owner)
    if cmd in ("/token", "/rugcheck"):
        import providers as P
        import token_intel as T
        if not args or not P.valid_address(args[0]):
            return send(owner, f"Usage: {cmd} &lt;token contract address&gt;")
        send(owner, "⏳ Analyzing… (market, holders, on-chain authorities, sell route" +
             (", recent trades)" if cmd == "/token" else ")"))
        cache = P.Cache(os.path.join(STATE_DIR, "pcache_best.json"))
        hist = T.History(os.path.join(STATE_DIR, "token_hist.json"))
        b = T.collect(args[0], cache, deep=True, trades=(cmd == "/token"))
        a = T.analyze(b, hist.get(args[0]), T.load_weights(os.path.join(STATE_DIR, "weights.json")))
        for t in (T.fmt_token(a) if cmd == "/token" else T.fmt_rugcheck(a)):
            send(owner, t)
        return
    if cmd.startswith("/watch") or cmd == "/unwatch":
        import watcher
        return send(owner, watcher.cmd(cmd, args))
    if cmd == "/notifications":
        import notify
        return send(owner, notify.command(args))
    if cmd in ("/copywatch", "/copytrade"):
        import copytrade
        fn = copytrade.cmd_copywatch if cmd == "/copywatch" else copytrade.cmd_copytrade
        return fn(args, lambda text, buttons=None: button_send(owner, text, buttons))


def on_callback(st, cq, send, button_send):
    owner = os.environ.get("TELEGRAM_CHAT_ID") or st.get("chat_id")
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    tgsend.call("answerCallbackQuery", dict(callback_query_id=cq.get("id")), timeout=10)
    if not owner or str(chat) != str(owner):
        return  # only the owner's chat may press buttons
    data = cq.get("data") or ""
    if not data.startswith("ct"):
        return
    import copytrade
    r = copytrade.on_button(data)
    if r == "CONFIRM":
        cfg, dat = copytrade.load_cfg(), copytrade.load(copytrade.p_data(), {})
        t, b = copytrade.turn_on_text(cfg, dat)
        return button_send(owner, t, b)
    send(owner, r)


def handle(st, u, send, button_send=None):
    st["offset"] = max(st.get("offset", 0), u.get("update_id", 0))
    button_send = button_send or (lambda chat, text, buttons=None: send(chat, text))
    if u.get("callback_query"):
        return on_callback(st, u["callback_query"], send, button_send)
    msg = u.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    owner = os.environ.get("TELEGRAM_CHAT_ID") or st.get("chat_id")
    if not owner and chat:
        st["chat_id"] = owner = str(chat)  # first chat to message the bot owns it
        send(owner, "🔒 Locked to this chat.\n" + HELP)
    if not chat or str(chat) != str(owner) or not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    cmd = {"/scan": "/best", "/copy": "/copywatch"}.get(cmd, cmd)
    if cmd in LIVE:
        try:
            live(cmd, args, send, owner, button_send)
        except Exception as e:  # a failing command must never kill the listener
            print(f"command {cmd} failed: {type(e).__name__}: {e}", flush=True)
            send(owner, f"Sorry, {cmd} failed this time ({type(e).__name__}). Try again in a moment.")
        return
    key = cmd
    if cmd == "/summary" and args and args[0].isdigit():
        key = f"/summary#{int(args[0])}"  # full breakdown of coin N
    snap = load(SNAP_PATH, {})
    replies = (snap.get("cmds") or {}).get(key)
    if replies is None:
        send(owner, ("Still warming up: first scan not finished yet. Try again in a minute."
                     if not snap else "Unknown command. /help lists them."))
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
        tgsend.send(text, chat)

    def button_send(chat, text, buttons=None):
        tgsend.send(text, chat, buttons)

    while time.time() < end:
        wait = max(1, min(25, int(end - time.time())))
        r = tgsend.call("getUpdates", {"offset": st.get("offset", 0) + 1, "timeout": wait,
                                       "allowed_updates": ["message", "callback_query"]}, timeout=wait + 10)
        for u in (r or {}).get("result") or []:
            handle(st, u, send, button_send)
            save(TG_PATH, st)
        save(TG_PATH, st)
        if r is None:
            time.sleep(3)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3300)
