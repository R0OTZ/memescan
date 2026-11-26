#!/usr/bin/env python3
"""Persistent token monitoring for /watch.

Runs as its own background process next to the listener. Every WATCH_INTERVAL seconds (default 10) it pulls
fresh market data for every active watched token in ONE batched DexScreener call (300 calls/min allowed, this
uses 6), compares it with the stored snapshots, and alerts only on meaningful changes. Every DEEP_SEC seconds
(default 300) it also re-reads holders and security (RugCheck + on-chain mint) to catch concentration changes,
deployer sells and authority changes.

Files (in the state folder, saved between hourly runs by the workflow cache):
  watch.json       the watch list: what you asked to watch, paused or not, thresholds. Written by commands.
  watch_data.json  snapshots, last check time, change history. Written only by this process.
  watcher.lock     heartbeat so a second copy never runs at the same time.
Usage: python3 watcher.py <seconds_to_run>
"""
import html
import json
import os
import sys
import time

import notify
import providers as P
import tgsend
import token_intel as T

INTERVAL = float(os.environ.get("WATCH_INTERVAL", "10"))
DEEP_SEC = float(os.environ.get("WATCH_DEEP_SEC", "300"))
KEEP_SEC = 3600
MAX_WATCH = 25


def sd():
    return tgsend.state_dir()


def p_cfg():
    return os.path.join(sd(), "watch.json")


def p_data():
    return os.path.join(sd(), "watch_data.json")


def p_lock():
    return os.path.join(sd(), "watcher.lock")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- change detection
def near(samples, now, sec):
    """Sample closest to `sec` seconds ago (within 40% tolerance)."""
    best = None
    for s in samples:
        d = abs((now - s["ts"]) - sec)
        if d <= max(15, sec * 0.4) and (best is None or d < best[0]):
            best = (d, s)
    return best[1] if best else None


def detect(samples, st):
    """Compare the newest sample with earlier ones. Returns a list of events (kind, sev, text)."""
    if len(samples) < 2:
        return []
    cur = samples[-1]
    now = cur["ts"]
    ev = []
    win10 = [s for s in samples if now - s["ts"] <= 600]
    hi_liq = max(s["liq"] for s in win10)
    if hi_liq > 0:
        drop = (1 - cur["liq"] / hi_liq) * 100
        if drop >= 50:
            ev.append(("rug", "critical", f"liquidity pulled: {drop:.0f}% gone in 10 min "
                                          f"({T.usd(hi_liq)} → {T.usd(cur['liq'])}). Possible rug."))
        elif drop >= st["liq_drop_pct"]:
            ev.append(("liq", "high", f"liquidity down {drop:.0f}% in 10 min ({T.usd(hi_liq)} → {T.usd(cur['liq'])})"))
    p1 = near(samples, now, 60)
    if p1 and p1["price"]:
        mv = (cur["price"] / p1["price"] - 1) * 100
        if abs(mv) >= st["price_move_pct"]:
            ev.append(("move1", "high" if mv < 0 else "info",
                       f"price {mv:+.0f}% in ~1 min ({T.px(p1['price'])} → {T.px(cur['price'])})"))
    p10 = near(samples, now, 600)
    if p10 and p10["price"] and not any(e[0] == "move1" for e in ev):
        mv = (cur["price"] / p10["price"] - 1) * 100
        if abs(mv) >= st["price_move_10m_pct"]:
            ev.append(("move10", "high" if mv < 0 else "info", f"price {mv:+.0f}% in 10 min, MC now {T.usd(cur['mcap'])}"))
    prev = samples[-2]

    def accel(x):
        return x["vol_m5"] * 12 / x["vol_h1"] if x["vol_h1"] > 0 else 0

    def spike(x):
        return accel(x) >= st["vol_accel"] and x["vol_m5"] >= st["min_vol_m5"]

    def selling(x):
        return x["s5"] >= st["min_sells_m5"] and x["s5"] >= st["sell_ratio"] * max(x["b5"], 1)

    def buying(x):
        return x["b5"] >= st["min_sells_m5"] and x["b5"] >= st["sell_ratio"] * max(x["s5"], 1)
    # condition alerts fire when the condition starts, not on every check while it lasts
    if spike(cur) and not spike(prev):
        ev.append(("vol", "info", f"unusual volume: last 5 min {T.usd(cur['vol_m5'])}, {accel(cur):.1f}x the hourly pace"))
    if selling(cur) and not selling(prev):
        ev.append(("sells", "high", f"selling pressure: {cur['s5']} sells vs {cur['b5']} buys in 5 min"))
    elif buying(cur) and not buying(prev):
        ev.append(("buys", "info", f"strong buying: {cur['b5']} buys vs {cur['s5']} sells in 5 min"))
    p30 = near(samples, now, 1800)
    if p30 and p30["tx_h1"] >= 50:
        chg = (cur["tx_h1"] / p30["tx_h1"] - 1) * 100
        if chg <= -st["activity_drop_pct"]:
            ev.append(("activity", "high", f"trading activity collapsing: 1h trades {p30['tx_h1']} → {cur['tx_h1']}"))
    return ev


def detect_deep(base, a, st):
    """Holder/security changes vs the baseline taken when watching started (or the last deep check)."""
    ev = []
    hd = a.get("holders")
    if hd and base.get("top10") is not None:
        d = hd["top10"] - base["top10"]
        if abs(d) >= st["top10_change_pts"]:
            ev.append(("conc", "high" if d > 0 else "info",
                       f"top-10 holder share {base['top10']:.1f}% → {hd['top10']:.1f}% "
                       f"({'concentrating' if d > 0 else 'spreading out'})"))
    if hd and base.get("dev_pct") and hd.get("dev_pct") is not None:
        drop = (1 - hd["dev_pct"] / base["dev_pct"]) * 100
        if drop >= st["dev_sell_pct"]:
            ev.append(("dev", "critical", f"deployer sold: holdings {base['dev_pct']:.2f}% → {hd['dev_pct']:.2f}% of supply"))
    if hd and base.get("cluster_pct") is not None and hd["cluster_pct"] - base["cluster_pct"] <= -5:
        ev.append(("cluster", "high", f"linked-wallet clusters reduced holdings {base['cluster_pct']:.1f}% → "
                                      f"{hd['cluster_pct']:.1f}% (possible coordinated selling)"))
    crit_now = {f["text"] for f in a["findings"] if f["sev"] == "critical"}
    new_crit = crit_now - set(base.get("critical") or [])
    for t in sorted(new_crit):
        ev.append(("security", "critical", "new security problem: " + t))
    if base.get("risk") is not None and a.get("risk") is not None and a["risk"] - base["risk"] >= 20:
        ev.append(("risk", "high", f"risk score jumped {base['risk']} → {a['risk']}"))
    return ev


def baseline(a):
    hd = a.get("holders") or {}
    return dict(t=a["t"], top10=hd.get("top10"), dev_pct=hd.get("dev_pct"), cluster_pct=hd.get("cluster_pct"),
                insider_pct=hd.get("insider_pct"), holders=hd.get("total"), risk=a.get("risk"),
                critical=[f["text"] for f in a["findings"] if f["sev"] == "critical"])


def sample_from_pair(p, now):
    m = T.pair_metrics(p, now)
    return dict(ts=now, price=m["price"], mcap=m["mcap"], liq=m["liq"], vol_m5=m["vol_m5"], vol_h1=m["vol_h1"],
                b5=m["b5"], s5=m["s5"], tx_h1=m["tx_h1"], sym=m["symbol"])


# ---------------------------------------------------------------- loop
class Watcher:
    def __init__(self, send=None, cache=None):
        self.send = send or (lambda text: tgsend.send(text))
        self.gate = notify.Gate("watcher")
        self.cache = cache or P.Cache(os.path.join(sd(), "pcache_watcher.json"))
        self.data = load(p_data(), {})

    def tick(self, now=None):
        now = now or time.time()
        cfg = load(p_cfg(), {})
        st = notify.load()
        active = [m for m, w in cfg.items() if w.get("active", True) and not w.get("paused")]
        for m in list(self.data):
            if m not in cfg:
                self.data.pop(m)  # unwatched
        if not active:
            return 0
        ds = P.ds_tokens(active, False)  # never cached: this is the 10-second feed
        sent = 0
        for mint in active:
            d = self.data.setdefault(mint, dict(samples=[], events=[], last_checked=None, last_deep=0,
                                                base=None, errors=0))
            pair = P.best_pair((ds.get("data") or {}).get(mint))
            if not pair:
                d["errors"] += 1
                d["last_error"] = ds.get("error") or "no pair data"
                continue
            s = sample_from_pair(pair, now)
            d["samples"] = [x for x in d["samples"] if now - x["ts"] < KEEP_SEC] + [s]
            d["last_checked"] = now
            d["sym"] = s["sym"]
            events = detect(d["samples"], st)
            if now - d.get("last_deep", 0) >= DEEP_SEC:
                d["last_deep"] = now
                b = T.collect(mint, self.cache, deep=False, ds=ds)
                a = T.analyze(b, [])
                if d.get("base"):
                    events += detect_deep(d["base"], a, st)
                d["base"] = baseline(a)
                d["last_risk"] = a["risk"]
            for kind, sev, text in events:
                d["events"] = (d["events"] + [dict(ts=now, kind=kind, sev=sev, text=text)])[-50:]
                cd = 5 if sev == "critical" else None
                if self.gate.allow("watch", f"{mint}:{kind}", cooldown_min=cd, now=now, settings=st,
                                   capped=sev not in ("critical", "high")):
                    icon = {"critical": "🚨", "high": "⚠️", "info": "📈"}.get(sev, "ℹ️")
                    self.send(f"{icon} <b>Watch: ${html.escape(s['sym'])}</b> · {T.clock(now)}\n{html.escape(text)}\n"
                              f"Price {T.px(s['price'])} · MC {T.usd(s['mcap'])} · Liq {T.usd(s['liq'])}\n"
                              f"<code>{mint}</code>\n<i>Why: change vs the bot's earlier snapshots crossed your "
                              f"threshold. /watchstatus {mint[:6]}… · /unwatch to stop</i>")
                    sent += 1
        save(p_data(), self.data)
        self.gate.save()
        self.cache.save()
        return sent


def acquire_lock(now=None):
    """Only one watcher at a time: refuse if another wrote a heartbeat in the last 30 s and is alive."""
    now = now or time.time()
    lk = load(p_lock(), {})
    if lk and now - lk.get("hb", 0) < 30 and lk.get("pid") != os.getpid():
        try:
            os.kill(int(lk["pid"]), 0)
            return False
        except (OSError, ValueError, TypeError):
            pass
    save(p_lock(), dict(pid=os.getpid(), hb=now))
    return True


COPY_SEC = float(os.environ.get("COPY_INTERVAL", "15"))


def copy_loop(end):
    """Copy-watch / copy-trade monitor in its own thread so slow RPC never delays the 10-second watch loop."""
    import copytrade
    cm = copytrade.CopyMonitor()
    while time.time() < end:
        t0 = time.time()
        try:
            cm.process_decisions()
            n = cm.tick()
            if n:
                print(f"copy: {n} alert(s)", flush=True)
        except Exception as e:
            print(f"copy tick failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(2.0, COPY_SEC - (time.time() - t0)))


BEST_EVERY = float(os.environ.get("BEST_EVERY", "120"))


def p_best():
    return os.path.join(sd(), "best.json")


def best_pass(cache, hist):
    """One /best discovery pass; results go to best.json for the listener (/best) and scanner (alerts, grading)."""
    import discovery as D
    res = D.run(cache, hist, T.load_weights(os.path.join(sd(), "weights.json")))
    out = dict(t=time.time(), mints=[a["mint"] for a in res["shown"]],
               tags={a["mint"]: a.get("tags", []) for a in res["shown"]}, n=res["n"], rejected=res["rejected"],
               src=res["src"], msgs=D.fmt(res), took=res["took"],
               shown=[dict(mint=a["mint"], sym=a["market"]["symbol"], price=a["market"]["price"], score=a["score"],
                           verdict=a["verdict"], cats={k: (round(v) if v is not None else None) for k, v in a["cats"].items()},
                           text=T.fmt_best_item(1, a).replace("#1 ", "", 1)) for a in res["shown"]])
    save(p_best(), out)
    cache.save()
    hist.save()
    return out


def best_loop(end):
    cache = P.Cache(os.path.join(sd(), "pcache_best.json"))
    hist = T.History(os.path.join(sd(), "token_hist.json"))
    while time.time() < end:
        t0 = time.time()
        try:
            r = best_pass(cache, hist)
            print(f"best: {r['n']} candidates, {len(r['shown'])} shown, {r['took']:.0f}s", flush=True)
        except Exception as e:
            print(f"best pass failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(10.0, BEST_EVERY - (time.time() - t0)))


def main(seconds):
    if not acquire_lock():
        print("watcher already running; exiting", flush=True)
        return
    import threading
    end = time.time() + seconds
    threading.Thread(target=copy_loop, args=(end,), daemon=True).start()
    threading.Thread(target=best_loop, args=(end,), daemon=True).start()
    w = Watcher()
    while time.time() < end:
        t0 = time.time()
        try:
            n = w.tick()
            if n:
                print(f"watch: {n} alert(s)", flush=True)
        except Exception as e:  # never die: log and keep watching
            print(f"watch tick failed: {type(e).__name__}: {e}", flush=True)
        save(p_lock(), dict(pid=os.getpid(), hb=time.time()))
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


# ---------------------------------------------------------------- commands (called by the listener)
def find(cfg, arg):
    if arg in cfg:
        return arg
    hits = [m for m in cfg if m.startswith(arg) or (cfg[m].get("sym") or "").lower() == arg.lower().lstrip("$")]
    return hits[0] if len(hits) == 1 else None


def cmd(name, args):
    cfg = load(p_cfg(), {})
    data = load(p_data(), {})
    arg = args[0] if args else ""
    if name == "/watchlist":
        if not cfg:
            return "Watch list is empty. /watch &lt;contract address&gt; to add one."
        rows = []
        for m, w in cfg.items():
            d = data.get(m) or {}
            last = d.get("last_checked")
            st = "⏸ paused" if w.get("paused") else ("✅ live" if last and time.time() - last < 60 else "⏳ starting")
            s = (d.get("samples") or [{}])[-1]
            rows.append(f"{st} <b>${html.escape(w.get('sym') or '?')}</b> MC {T.usd(s.get('mcap'))} · "
                        f"checked {T.ago(time.time() - last) + ' ago' if last else 'not yet'} · "
                        f"{len(d.get('events') or [])} changes\n<code>{m}</code>")
        return f"👀 <b>Watching {len(cfg)}</b> (every {INTERVAL:.0f}s)\n" + "\n".join(rows)
    if not arg:
        return f"Usage: {name} &lt;contract address&gt;"
    if name == "/watch":
        if not P.valid_address(arg):
            return "That isn't a valid Solana address."
        if arg in cfg:
            return f"Already watching ${html.escape(cfg[arg].get('sym') or '?')}. /watchstatus to see it."
        if len(cfg) >= MAX_WATCH:
            return f"Watch list is full ({MAX_WATCH}). /unwatch one first."
        ds = P.ds_tokens([arg])
        pair = P.best_pair((ds.get("data") or {}).get(arg))
        if not ds["ok"]:
            return "DexScreener didn't answer, so I can't confirm the token. Try again in a moment."
        if not pair:
            return "No trading pair found for that address on DexScreener. Is it a token contract?"
        m = T.pair_metrics(pair, time.time())
        cfg[arg] = dict(sym=m["symbol"], started=time.time(), active=True, paused=False)
        save(p_cfg(), cfg)
        st = notify.load()
        warn = "" if st["enabled"] and st["cats"]["watch"] else \
            "\n⚠️ Watch alerts are currently OFF: /notifications on watch to receive them."
        return (f"👀 Now watching <b>${html.escape(m['symbol'])}</b> every {INTERVAL:.0f}s.\n"
                f"MC {T.usd(m['mcap'])} · Liq {T.usd(m['liq'])} · Price {T.px(m['price'])}\n"
                f"I'll alert only on meaningful changes (liquidity drop ≥{st['liq_drop_pct']:.0f}%, price ±"
                f"{st['price_move_pct']:.0f}%/1m, volume spikes, sell pressure, holder or security changes)."
                f"{warn}\n<code>{arg}</code>")
    m = find(cfg, arg)
    if not m:
        return "That token isn't on your watch list. /watchlist shows it."
    w = cfg[m]
    if name == "/unwatch":
        cfg.pop(m)
        save(p_cfg(), cfg)
        return f"Stopped watching ${html.escape(w.get('sym') or '?')}."
    if name in ("/watchpause", "/watchresume"):
        w["paused"] = name == "/watchpause"
        save(p_cfg(), cfg)
        return f"{'⏸ Paused' if w['paused'] else '▶️ Resumed'} ${html.escape(w.get('sym') or '?')}."
    if name == "/watchstatus":
        d = data.get(m) or {}
        ss = d.get("samples") or []
        if not ss:
            return f"${html.escape(w.get('sym') or '?')}: no data yet (first check within {INTERVAL:.0f}s)."
        cur, now = ss[-1], time.time()
        L = [f"👀 <b>${html.escape(w.get('sym') or '?')}</b> · {'paused' if w.get('paused') else 'live'} since "
             f"{T.clock(w['started'])}",
             f"<code>{m}</code>",
             f"Last check {T.ago(now - cur['ts'])} ago ({P.freshness(now - cur['ts'])})",
             f"Price {T.px(cur['price'])} · MC {T.usd(cur['mcap'])} · Liq {T.usd(cur['liq'])}",
             f"5m: {cur['b5']} buys / {cur['s5']} sells · vol {T.usd(cur['vol_m5'])} · 1h trades {cur['tx_h1']}"]
        for sec, lab in ((60, "1m"), (600, "10m"), (1800, "30m")):
            o = near(ss, cur["ts"], sec)
            if o and o["price"]:
                L.append(f"vs {lab} ago: price {((cur['price'] / o['price']) - 1) * 100:+.1f}% · liq "
                         f"{((cur['liq'] / o['liq']) - 1) * 100 if o['liq'] else 0:+.1f}%")
        b = d.get("base")
        if b:
            L.append(f"Holders/security (checked {T.ago(now - b['t'])} ago): top10 "
                     + (f"{b['top10']:.1f}%" if b.get("top10") is not None else "?")
                     + f" · risk {b.get('risk') if b.get('risk') is not None else '?'}/100")
        ev = d.get("events") or []
        L.append(f"<b>Changes detected ({len(ev)})</b>" + ("".join(
            f"\n{T.clock(e['ts'])}: {html.escape(e['text'])}" for e in ev[-6:]) or "\nnone yet"))
        if d.get("last_error"):
            L.append(f"Last data error: {html.escape(d['last_error'])}")
        return "\n".join(L)
    return "Unknown watch command."


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3300)
