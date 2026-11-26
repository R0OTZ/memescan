"""Autonomous notifications, kept separate from command replies.

Command replies (/best, /token, /rugcheck, /summary, /stats, /watchlist, /watchstatus...) are answered in the
chat and never go through here. Only background systems call allow() before sending: watch alerts, position
alerts, copy-trade alerts, discovery alerts and the periodic digest. Each has an on/off switch, a cooldown per
(token, kind) and an hourly cap, all stored in state/notify.json so they survive restarts.
"""
import json
import os
import time

CATS = dict(watch="Watched tokens (/watch)", positions="Your holdings (stop, take-profit, rug)",
            copy="Copy-trade wallets", discovery="New opportunities meeting your criteria", digest="2-hourly summary")
DEFAULTS = dict(
    enabled=True,
    cats={k: True for k in CATS},
    cooldown_min=15,          # same token + same alert kind
    max_per_hour=20,          # non-urgent autonomous alerts together (exit/rug alerts are never capped)
    # /watch thresholds
    liq_drop_pct=15.0,        # liquidity down this much within 10 min
    price_move_pct=12.0,      # price moves this much within 1 min (either way)
    price_move_10m_pct=25.0,  # ... or within 10 min
    vol_accel=3.0,            # last-5m volume pace vs the 1h pace
    min_vol_m5=1500.0,        # ...and at least this much volume in 5 min
    sell_ratio=2.0,           # sells/buys in the last 5 min
    min_sells_m5=15,
    activity_drop_pct=60.0,   # 1h trade count down this much vs 30 min ago
    top10_change_pts=5.0,     # top-10 holder share changes this many points
    dev_sell_pct=20.0,        # deployer balance falls this much
    # discovery
    discovery_min_score=70,
)
ALLOWED_KEYS = [k for k in DEFAULTS if k not in ("enabled", "cats")]


def _dir():
    return os.path.dirname(os.environ.get("STATE_PATH", "state/state_v2.json")) or "."


def path():
    return os.path.join(_dir(), "notify.json")


def load():
    try:
        with open(path()) as f:
            s = json.load(f)
    except Exception:
        s = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in s.items():
        if k == "cats" and isinstance(v, dict):
            out["cats"].update({c: bool(x) for c, x in v.items() if c in CATS})
        elif k in out:
            out[k] = v
    return out


def save(s):
    os.makedirs(_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, path())


class Gate:
    """Cooldown + dedupe + hourly cap for one process. log_name keeps each process's send log separate so
    two processes never overwrite each other's file."""

    def __init__(self, log_name):
        self.log_path = os.path.join(_dir(), f"sent_{log_name}.json")
        try:
            with open(self.log_path) as f:
                self.log = json.load(f)
        except Exception:
            self.log = {"keys": {}, "times": []}

    def allow(self, cat, key, cooldown_min=None, now=None, settings=None, capped=True):
        """True if an alert of category cat with dedupe key may be sent now (and records it).
        capped=False for urgent alerts (exits, rugs on your holdings, critical watch events): they still respect
        the on/off switches and dedupe, but never wait behind the hourly cap."""
        s = settings or load()
        now = now or time.time()
        if not s["enabled"] or not s["cats"].get(cat, True):
            return False
        cd = (cooldown_min if cooldown_min is not None else s["cooldown_min"]) * 60
        if now - self.log["keys"].get(key, 0) < cd:
            return False
        times = [t for t in self.log["times"] if now - t < 3600]
        if capped and len(times) >= s["max_per_hour"]:
            return False
        self.log["keys"][key] = now
        self.log["times"] = times + [now]
        return True

    def save(self):
        now = time.time()
        self.log["keys"] = {k: v for k, v in self.log["keys"].items() if now - v < 3 * 86400}
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        with open(self.log_path + ".tmp", "w") as f:
            json.dump(self.log, f)
        os.replace(self.log_path + ".tmp", self.log_path)


def command(args):
    """/notifications on|off|status|settings [...]. Returns reply text (HTML)."""
    s = load()
    a = [x.lower() for x in args]
    if not a or a[0] == "status":
        on = "ON" if s["enabled"] else "OFF"
        cats = "\n".join(f"{'✅' if s['cats'][k] else '⛔'} {k}: {v}" for k, v in CATS.items())
        return (f"🔔 <b>Notifications: {on}</b>\n{cats}\nCooldown {s['cooldown_min']} min per token and alert type · "
                f"max {s['max_per_hour']}/hour\nCommands never trigger these. /notifications settings to tune.")
    if a[0] in ("on", "off"):
        if len(a) >= 2 and a[1] in CATS:
            s["cats"][a[1]] = a[0] == "on"
            save(s)
            return f"{'✅' if a[0] == 'on' else '⛔'} {a[1]} alerts {a[0].upper()}."
        s["enabled"] = a[0] == "on"
        save(s)
        return f"🔔 All autonomous notifications {a[0].upper()}." + (
            "" if s["enabled"] else " Commands still answer normally; background alerts are held.")
    if a[0] == "settings":
        if len(a) >= 3 and a[1] in ALLOWED_KEYS:
            try:
                val = float(a[2])
            except ValueError:
                return "Value must be a number."
            if val < 0:
                return "Value must be 0 or more."
            s[a[1]] = int(val) if isinstance(DEFAULTS[a[1]], int) else val
            save(s)
            return f"Set {a[1]} = {s[a[1]]}."
        rows = "\n".join(f"{k} = {s[k]}" for k in ALLOWED_KEYS)
        return ("<b>Notification settings</b>\n<pre>" + rows + "</pre>\nChange one: /notifications settings "
                "liq_drop_pct 20\nToggle a type: /notifications off digest · /notifications on watch")
    return "Use /notifications on | off | status | settings"
