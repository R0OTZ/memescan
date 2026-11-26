"""Copy-watch (track traders) and copy-trade (one selected wallet, ON/OFF, simulated fills).

Wallet addresses are only ever ones you supply (copied from the trader's fomo profile). The bot checks each is a
real, active Solana wallet, but cannot confirm who owns it: that's shown as "address supplied by you".

Trade detection decodes each transaction's balance changes for the wallet (portfolio.parse_tx) and classifies it:
buy / add-to-position / partial sell / full exit / token-to-token swap / transfer in / transfer out /
liquidity add / liquidity remove / failed. Only buys and sells are copy candidates. Signatures are stored so
nothing is processed twice; new transactions are processed oldest-first.

Copy trading here is SIMULATED: when ON, every safety check and spending limit runs, and the "fill" is a real
Jupiter quote for your configured size at that moment, recorded as a paper trade. No private key is stored and
no transaction is ever sent. Every message about it says SIMULATED.

Files: copy.json (config: written by commands), copy_data.json (history: written by the watcher process).
"""
import html
import json
import os
import time

import notify
import portfolio as PF
import providers as P
import tgsend
import token_intel as T

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USD_STABLES = (USDC, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWXJN3YUiR1GaEz", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWX2ZCG7FrPKdDhUmJ")
DEFAULT_TRADERS = ["SavageCountry", "orangie", "rasmr", "RearBlindGull", "end837"]
SETTINGS = dict(
    bankroll=20.0,          # simulated starting balance (USD)
    max_per_trade=2.0,      # USD per copied buy (fomo minimum is $2)
    max_allocation=10.0,    # USD across all open copied positions
    max_daily=10.0,         # USD of copied buys per day (ET)
    max_slippage_pct=5.0,   # quote price impact allowed
    max_deviation_pct=15.0, # how far price may have moved since the source trade
    min_liquidity=15000.0,
    max_risk=45.0,          # token_intel risk score ceiling (0-100)
    max_age_sec=180.0,      # ignore source trades older than this (don't chase stale signals)
    copy_buys=1, copy_adds=1, copy_partial=1, copy_full=1, require_confirm=0)
BOOL_KEYS = ("copy_buys", "copy_adds", "copy_partial", "copy_full", "require_confirm")


def sd():
    return tgsend.state_dir()


def p_cfg():
    return os.path.join(sd(), "copy.json")


def p_data():
    return os.path.join(sd(), "copy_data.json")


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


def load_cfg():
    c = load(p_cfg(), {})
    tr = c.setdefault("traders", {})
    env = [tuple(x.strip().split(":", 1)) for x in os.environ.get("COPY_WALLETS", "").split(",") if ":" in x]
    for n in DEFAULT_TRADERS + [n for n, _ in env]:
        tr.setdefault(n.lower(), dict(name=n, wallets=[]))
    for n, a in env:
        ws = tr[n.lower()]["wallets"]
        if a and not any(w["addr"] == a for w in ws):
            ws.append(dict(addr=a, source="COPY_WALLETS setting", added=time.time()))
    ct = c.setdefault("copytrade", {})
    ct.setdefault("wallet", None)
    ct.setdefault("state", "off")          # off | on | paused. Default OFF; nothing but you turns it on.
    ct.setdefault("ever_enabled", False)
    ct.setdefault("settings", {})
    for k, v in SETTINGS.items():
        ct["settings"].setdefault(k, v)
    ct.setdefault("decisions", {})          # pending confirm-before-execute answers from buttons
    return c


def short(a):
    return T.short(a)


def solscan_tx(sig):
    return f"https://solscan.io/tx/{sig}"


def solscan_acct(a):
    return f"https://solscan.io/account/{a}"


# ---------------------------------------------------------------- decoding
def rpc_fn(m, p):
    r = P.rpc(m, p)
    return r["data"] if r["ok"] else None


def kind_of(ev):
    """Finer classification than portfolio.classify: separates liquidity operations from trades."""
    if ev.get("failed"):
        return "failed"
    if ev.get("skip"):
        return "other"
    t = ev["tokens"]
    ins = [m for m, d in t.items() if d > 0]
    outs = [m for m, d in t.items() if d < 0]
    paid = ev["stable"] < -0.005 or ev["sol"] < -1e-5
    got = ev["stable"] > 0.005 or ev["sol"] > 1e-5
    if ins and outs:
        return "swap"
    if ins:
        return "buy" if paid else "liquidity_remove" if got else "transfer_in"
    if outs:
        return "sell" if got else "liquidity_add" if paid else "transfer_out"
    return "deposit" if got else "withdraw" if paid else "other"


def decode(tx, owner):
    ev = PF.parse_tx(tx, owner, set(USD_STABLES))
    if ev is None:
        return dict(ts=tx.get("blockTime") or 0, failed=True, tokens={}, stable=0.0, sol=0.0, fee=0.0)
    # if the wallet's SOL balance didn't actually move, someone else paid fees/rent (fomo sponsors them):
    # don't let portfolio's rent/fee adjustments turn that into a SOL payment or receipt
    meta = tx.get("meta") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    if owner in keys:
        i = keys.index(owner)
        try:
            raw = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        except (KeyError, IndexError, TypeError):
            raw = None
        if raw == 0:
            ev["sol"] = 0.0
    post = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == owner and b.get("mint") in ev["tokens"]:
            post[b["mint"]] = post.get(b["mint"], 0.0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    ev["post"] = post
    return ev


def poll(wd, addr, budget, now):
    """Fetch new signatures (and some older history), decode up to `budget` transactions. Returns the list of
    newly decoded events that happened after monitoring started, oldest first."""
    wd.setdefault("events", {})
    wd.setdefault("pending", [])
    wd.setdefault("start", now)
    wd.setdefault("complete", False)
    opts = {"limit": 50}
    if wd.get("newest"):
        opts["until"] = wd["newest"]
    new = rpc_fn("getSignaturesForAddress", [addr, opts])
    if new is None:
        wd["rpc_errors"] = wd.get("rpc_errors", 0) + 1
        return []
    wd["last_poll"] = now
    if new:
        wd["newest"] = new[0]["signature"]
        wd.setdefault("oldest", new[-1]["signature"])
        fresh = [[s["signature"], s.get("blockTime") or 0, bool(s.get("err"))] for s in reversed(new)]
        wd["pending"] = fresh + [p for p in wd["pending"] if p[0] not in {f[0] for f in fresh}]
    if not wd["complete"] and wd.get("oldest") and len(wd["pending"]) < 20 and len(wd["events"]) < 600:
        old = rpc_fn("getSignaturesForAddress", [addr, {"limit": 50, "before": wd["oldest"]}])
        if old is not None:
            if old:
                wd["oldest"] = old[-1]["signature"]
                wd["pending"] += [[s["signature"], s.get("blockTime") or 0, bool(s.get("err"))] for s in old]
            if len(old) < 50:
                wd["complete"] = True
    if len(wd["events"]) >= 600:
        wd["complete"] = True  # profile cap: enough history for stats; new trades keep coming in
    out, done = [], 0
    # newest-first queue for live trades: process anything after `start` before backfill, oldest of them first
    live = sorted([p for p in wd["pending"] if p[1] >= wd["start"] - 5], key=lambda p: p[1])
    rest = [p for p in wd["pending"] if p not in live]
    queue = live + rest
    keep = []
    for sig, bt, err in queue:
        if sig in wd["events"]:
            continue
        if done >= budget:
            keep.append([sig, bt, err])
            continue
        if err:
            ev = dict(ts=bt, failed=True, tokens={}, stable=0.0, sol=0.0, fee=0.0)
        else:
            tx = rpc_fn("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            done += 1
            if tx is None:
                keep.append([sig, bt, err])
                continue
            ev = decode(tx, addr)
        ev["kind"] = kind_of(ev)
        ev["sig"] = sig
        wd["events"][sig] = ev
        if ev["ts"] >= wd["start"] - 5:
            out.append(ev)
    wd["pending"] = keep
    return sorted(out, key=lambda e: e["ts"])


def holdings_before(wd, mint, ts):
    """Was this wallet already holding mint before ts? (from its decoded history)"""
    qty = 0.0
    for e in sorted(wd["events"].values(), key=lambda e: e["ts"]):
        if e["ts"] >= ts:
            break
        qty += (e.get("tokens") or {}).get(mint, 0.0)
    return qty


def usd_leg(ev, sol_price):
    return ev["stable"] + ev["sol"] * (sol_price or 0)


# ---------------------------------------------------------------- profiles
def profile(wd, sol_px_store, mkt=None):
    """Realized-only trading profile from the decoded history. mkt: {mint: pair_metrics} for timing/risk."""
    pf = dict(events={s: e for s, e in wd.get("events", {}).items() if not e.get("failed")},
              solpx=sol_px_store)
    L = PF.ledger(pf)
    sells = [t for t in L["trades"] if t["kind"] == "sell" and t["pnl"] is not None]
    buys = [t for t in L["trades"] if t["kind"] == "buy"]
    evs = sorted(wd.get("events", {}).values(), key=lambda e: e["ts"])
    span_d = max(1 / 24, (evs[-1]["ts"] - evs[0]["ts"]) / 86400) if len(evs) >= 2 else None
    wins = [t for t in sells if t["pnl"] > 0]
    out = dict(n_events=len(evs), buys=len(buys), closed=len(sells), complete=wd.get("complete"),
               win_rate=(len(wins) / len(sells)) if sells else None,
               avg_ret=(sum(t["pct"] for t in sells if t["pct"] is not None) /
                        max(1, sum(1 for t in sells if t["pct"] is not None))) if sells else None,
               realized=sum(t["pnl"] for t in sells),
               avg_hold_min=(sum(t["held"] for t in sells if t["held"]) / max(1, sum(1 for t in sells if t["held"])) / 60)
               if sells else None,
               best=max(sells, key=lambda t: t["pnl"]) if sells else None,
               worst=min(sells, key=lambda t: t["pnl"]) if sells else None,
               per_day=(len(buys) / span_d) if span_d else None, span_d=span_d,
               unpriced=L["totals"]["unpriced"], unknown_basis=L["totals"]["unknown_basis"],
               first_ts=evs[0]["ts"] if evs else None)
    mints = list(dict.fromkeys(t["mint"] for t in buys))
    out["pump_share"] = (sum(1 for m in mints if m.endswith("pump")) / len(mints)) if mints else None
    if mkt:
        ages = []
        for t in buys:
            m = mkt.get(t["mint"])
            if m and m.get("age_h") is not None:
                created = time.time() - m["age_h"] * 3600
                if t["ts"] >= created:
                    ages.append((t["ts"] - created) / 60)
        ages.sort()
        out["entry_after_launch_min"] = ages[len(ages) // 2] if ages else None
        known = [m for m in mints if m in mkt]
        out["risky_share"] = (sum(1 for m in mints if m not in mkt or mkt[m]["liq"] < 20000) / len(mints)) if mints else None
        out["dead_share"] = (sum(1 for m in mints if m not in mkt) / len(mints)) if mints else None
        out["n_known"] = len(known)
    return out


def profile_text(name, addr, pr):
    e = html.escape
    L = [f"📊 <b>{e(name)}</b> <code>{e(addr)}</code>",
         f"History loaded: {pr['n_events']} transactions" + ("" if pr["complete"] else " (still loading older history)")
         + (f" over {pr['span_d']:.0f} days" if pr["span_d"] and pr["span_d"] >= 1 else "")]
    if pr["closed"] < 5:
        L.append(f"Only {pr['closed']} closed trades with a known cost so far: too few to judge this wallet.")
    else:
        L.append(f"Realized win rate <b>{pr['win_rate']:.0%}</b> over {pr['closed']} closed trades · "
                 f"avg return {T.pct(pr['avg_ret'])} · realized {T.usd(pr['realized'])}")
        L.append(f"Avg hold {pr['avg_hold_min']:.0f} min · ~{pr['per_day']:.1f} buys/day")
        if pr["best"]:
            L.append(f"Largest gain {T.usd(pr['best']['pnl'])} ({T.pct(pr['best']['pct'])}) · largest loss "
                     f"{T.usd(pr['worst']['pnl'])} ({T.pct(pr['worst']['pct'])})")
    if pr.get("entry_after_launch_min") is not None:
        m = pr["entry_after_launch_min"]
        L.append(f"Typical entry: {m:.0f} min after the pair launched ({'early' if m < 60 else 'mid' if m < 1440 else 'late'})")
    if pr.get("pump_share") is not None:
        L.append(f"{pr['pump_share']:.0%} of coins traded are pump.fun launches")
    if pr.get("risky_share") is not None:
        L.append(f"{pr['risky_share']:.0%} of coins traded now have under $20k liquidity or no market (high-risk style)")
    if pr["unpriced"] or pr["unknown_basis"]:
        L.append(f"<i>{pr['unpriced']} trades couldn't be priced and {pr['unknown_basis']} sells had no known cost; "
                 f"they're left out, not guessed.</i>")
    L.append("<i>Realized results only. Past wins don't make a wallet profitable to copy.</i>")
    return "\n".join(L)


# ---------------------------------------------------------------- simulated execution
def jup_quote(inp, out, amount_raw):
    r = P.http_json(f"{P.JUP}?inputMint={inp}&outputMint={out}&amount={int(amount_raw)}&slippageBps=1000",
                    tries=2, source="Jupiter")
    return r["data"] if r["ok"] and isinstance(r["data"], dict) and r["data"].get("routePlan") else None


def day_key(ts):
    return PF.day_of(ts)


def checks_buy(ct, cd, ev, mint, a, now, src_price):
    """Every reason not to copy this buy. Empty list = OK."""
    s = ct["settings"]
    why = []
    mk = a.get("market")
    if now - ev["ts"] > s["max_age_sec"]:
        why.append(f"source trade is {T.ago(now - ev['ts'])} old (limit {s['max_age_sec']:.0f}s)")
    if ev["sig"] in cd.get("copied", {}):
        why.append("duplicate: this source transaction was already handled")
    if not mk:
        why.append("no market data: token not tradable yet")
        return why
    if mk["liq"] < s["min_liquidity"]:
        why.append(f"liquidity {T.usd(mk['liq'])} under your {T.usd(s['min_liquidity'])} minimum")
    if a["disqualified"]:
        why.append("failed security: " + "; ".join(f["text"] for f in a["findings"] if f["sev"] == "critical")[:150])
    elif a["risk"] is None:
        why.append("security couldn't be checked (sources down)")
    elif a["risk"] > s["max_risk"]:
        why.append(f"risk {a['risk']}/100 above your limit {s['max_risk']:.0f}")
    if a.get("route") is not None and not a["route"].get("route"):
        why.append("no sell route on Jupiter (possible honeypot)")
    if src_price and mk["price"]:
        dev = (mk["price"] / src_price - 1) * 100
        if abs(dev) > s["max_deviation_pct"]:
            why.append(f"price moved {dev:+.0f}% since the source trade (limit ±{s['max_deviation_pct']:.0f}%)")
    amt = s["max_per_trade"]
    spent_today = cd.get("daily", {}).get(day_key(now), 0.0)
    if spent_today + amt > s["max_daily"] + 1e-9:
        why.append(f"daily limit: {T.usd(spent_today)} of {T.usd(s['max_daily'])} used")
    open_cost = sum(p["cost"] for p in cd.get("positions", {}).values())
    if open_cost + amt > s["max_allocation"] + 1e-9:
        why.append(f"allocation limit: {T.usd(open_cost)} of {T.usd(s['max_allocation'])} in open copies")
    if cd.get("cash", s["bankroll"]) < amt:
        why.append(f"simulated balance {T.usd(cd.get('cash', s['bankroll']))} below {T.usd(amt)}")
    return why


def sim_buy(cd, mint, usd_amt, decimals, now, sym, sig, max_slip=None):
    q = jup_quote(USDC, mint, usd_amt * 1e6)
    if not q:
        return None, "no Jupiter quote: can't price a fill"
    if max_slip is not None and float(q.get("priceImpactPct") or 0) * 100 > max_slip:
        return None, f"price impact {float(q.get('priceImpactPct') or 0) * 100:.1f}% above your {max_slip}% slippage limit"
    qty = int(q["outAmount"]) / 10 ** decimals
    impact = float(q.get("priceImpactPct") or 0) * 100
    pos = cd.setdefault("positions", {}).setdefault(mint, dict(qty=0.0, cost=0.0, sym=sym, opened=now))
    pos["qty"] += qty
    pos["cost"] += usd_amt
    cd["cash"] = cd.get("cash", 0.0) - usd_amt
    cd.setdefault("daily", {})[day_key(now)] = cd.get("daily", {}).get(day_key(now), 0.0) + usd_amt
    fill = dict(ts=now, side="buy", mint=mint, sym=sym, usd=usd_amt, qty=qty, price=usd_amt / qty if qty else None,
                impact=impact, source_sig=sig, simulated=True)
    cd.setdefault("fills", []).append(fill)
    return fill, None


def sim_sell(cd, mint, frac, decimals, now, sig):
    pos = cd.get("positions", {}).get(mint)
    if not pos or pos["qty"] <= 0:
        return None, "no simulated position in this token"
    qty = pos["qty"] * min(1.0, frac)
    q = jup_quote(mint, USDC, qty * 10 ** decimals)
    if not q:
        return None, "no Jupiter quote: can't price a sale (illiquid?)"
    proceeds = int(q["outAmount"]) / 1e6
    basis = pos["cost"] * qty / pos["qty"]
    pos["qty"] -= qty
    pos["cost"] -= basis
    cd["cash"] = cd.get("cash", 0.0) + proceeds
    fill = dict(ts=now, side="sell", mint=mint, sym=pos["sym"], usd=proceeds, qty=qty, price=proceeds / qty if qty else None,
                pnl=proceeds - basis, impact=float(q.get("priceImpactPct") or 0) * 100, source_sig=sig, simulated=True)
    cd.setdefault("fills", []).append(fill)
    if pos["qty"] <= 1e-12 or pos["cost"] <= 1e-9:
        cd["positions"].pop(mint, None)
    return fill, None


# ---------------------------------------------------------------- the monitor (runs inside watcher.py)
class CopyMonitor:
    def __init__(self, send=None, cache=None):
        self.send = send or (lambda text, buttons=None: tgsend.send(text, buttons=buttons))
        self.gate = notify.Gate("copy")
        self.cache = cache or P.Cache()
        self.data = load(p_data(), {})
        self.rr = 0  # round-robin pointer for backfill budget

    def sol_price(self):
        r = P.ds_tokens([P.WSOL], self.cache)
        best = None
        for p in (r.get("data") or {}).get(P.WSOL, []):
            if (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT"):
                if not best or float((p.get("liquidity") or {}).get("usd") or 0) > float((best.get("liquidity") or {}).get("usd") or 0):
                    best = p
        return float(best["priceUsd"]) if best else None

    def tick(self, now=None, budget=6):
        now = now or time.time()
        cfg = load_cfg()
        ct = cfg["copytrade"]
        targets = {}
        for t in cfg["traders"].values():
            for w in t["wallets"]:
                targets.setdefault(w["addr"], t["name"])
        if ct.get("wallet"):
            targets.setdefault(ct["wallet"], ct.get("label") or "copy wallet")
        if not targets:
            return 0
        sp = None
        wallets = self.data.setdefault("wallets", {})
        sent = 0
        addrs = list(targets)
        self.rr = (self.rr + 1) % len(addrs)
        for i, addr in enumerate(addrs):
            wd = wallets.setdefault(addr, {})
            new = poll(wd, addr, budget if i == self.rr else 2, now)
            for ev in new:
                if ev["kind"] not in ("buy", "sell", "swap", "failed"):
                    continue
                if sp is None:
                    sp = self.sol_price()
                sent += self.handle(addr, targets[addr], wd, ev, cfg, now, sp)
        if now - self.data.get("solpx_ts", 0) > 1800:  # hourly SOL prices so SOL-paid trades can be valued
            need = [e["ts"] for wd in wallets.values() for e in (wd.get("events") or {}).values()
                    if e.get("sol") and not e.get("failed")]
            if need:
                holder = dict(solpx=self.data.setdefault("solpx", {}))
                try:
                    PF.ensure_sol_prices(holder, need, lambda u: P.http_json(u, tries=1)["data"])
                    self.data["solpx_ts"] = now
                except Exception:
                    pass
        # wallets no longer tracked: drop their data
        for a in [a for a in wallets if a not in targets]:
            wallets.pop(a)
        save(p_data(), self.data)
        self.gate.save()
        return sent

    def handle(self, addr, name, wd, ev, cfg, now, sol_price):
        ct = cfg["copytrade"]
        is_ct = addr == ct.get("wallet")
        st = notify.load()
        e = html.escape
        log = self.data.setdefault("detected", [])
        if ev["kind"] == "failed":
            log.append(dict(ts=ev["ts"], who=name, addr=addr, kind="failed", sig=ev["sig"]))
            return 0
        if ev["kind"] == "swap":
            log.append(dict(ts=ev["ts"], who=name, addr=addr, kind="swap", sig=ev["sig"]))
            return 0
        mint = next((m for m, d in ev["tokens"].items() if (d > 0) == (ev["kind"] == "buy")), None)
        if not mint:
            return 0
        size = abs(usd_leg(ev, sol_price))
        qty = abs(ev["tokens"][mint])
        src_price = size / qty if qty else None
        b = T.collect(mint, self.cache, deep=True, trades=False)
        a = T.analyze(b, [])
        mk = a.get("market") or {}
        sym = mk.get("symbol") or short(mint)
        if ev["kind"] == "buy":
            before = holdings_before(wd, mint, ev["ts"])
            sub = "added to position" if before > 0 else "new position"
        else:
            post = (ev.get("post") or {}).get(mint, 0.0)
            frac = qty / (qty + post) if (qty + post) else 1.0
            sub = "FULL EXIT" if post <= 1e-9 else f"partial sell ({frac:.0%} of their bag)"
        log.append(dict(ts=ev["ts"], who=name, addr=addr, kind=ev["kind"], sub=sub, mint=mint, sym=sym, usd=size,
                        sig=ev["sig"]))
        self.data["detected"] = log[-300:]
        pr = profile(wd, self.data.get("solpx", {}))
        hist_line = ("Wallet history: " + (f"realized win rate {pr['win_rate']:.0%} over {pr['closed']} closed trades"
                                           if pr["closed"] >= 5 else f"only {pr['closed']} closed trades loaded: not enough to judge"))
        head = (f"👥 <b>{'SOURCE WALLET ' if is_ct else ''}{e(name)} {'BOUGHT' if ev['kind'] == 'buy' else 'SOLD'} ${e(sym)}</b>"
                f" · {sub}\n{T.clock(ev['ts'])} · ~{T.usd(size)} · <a href=\"{solscan_tx(ev['sig'])}\">source tx</a>\n"
                f"Wallet <code>{e(addr)}</code> (address supplied by you)\n<code>{e(mint)}</code>\n"
                + (f"MC {T.usd(mk.get('mcap'))} · Liq {T.usd(mk.get('liq'))} · " if mk else "No market data yet · ")
                + f"risk {a['risk'] if a['risk'] is not None else '?'}/100 ({a['risk_band']})\n{hist_line}")
        out = 0
        if not is_ct:
            if self.gate.allow("copy", f"{addr}:{ev['sig']}", cooldown_min=0, now=now, settings=st):
                self.send(head + "\n<i>Alert only: nothing is bought for you.</i>")
                out += 1
            return out
        # ---- selected copy-trade wallet
        cd = self.data.setdefault("copytrade", {})
        cd.setdefault("cash", ct["settings"]["bankroll"])
        cd.setdefault("copied", {})
        cd.setdefault("skipped", [])
        cd.setdefault("detected_n", 0)
        cd["detected_n"] += 1
        cd["last_detected"] = dict(ts=ev["ts"], sig=ev["sig"], sym=sym, kind=ev["kind"], sub=sub)
        s = ct["settings"]
        state = ct.get("state", "off")
        dec = (b.get("chain") or {}).get("decimals") or (a.get("holders") or {}).get("decimals")
        status = None
        if state != "on":
            status = f"not copied: copy trading is {state.upper()}"
        elif ev["kind"] == "buy":
            want = s["copy_adds"] if sub == "added to position" else s["copy_buys"]
            why = [] if want else [f"{sub}: copying this type is turned off in settings"]
            why += checks_buy(ct, cd, ev, mint, a, now, src_price)
            if dec is None:
                why.append("token decimals unknown")
            if not why and s["require_confirm"]:
                pend = cd.setdefault("pending", {})
                pend[ev["sig"][:16]] = dict(sig=ev["sig"], mint=mint, ts=now, sym=sym, src_price=src_price,
                                            ev_ts=ev["ts"], decimals=dec)
                self.send(head + f"\n\n🟡 Passes every check. Simulate a {T.usd(s['max_per_trade'])} copy?",
                          [[dict(text="✅ Copy (simulated)", callback_data=f"ctc:{ev['sig'][:16]}:y"),
                            dict(text="❌ Skip", callback_data=f"ctc:{ev['sig'][:16]}:n")]])
                return 1
            if why:
                status = "SKIPPED: " + "; ".join(why)
                cd["skipped"].append(dict(ts=now, sig=ev["sig"], sym=sym, why=why))
            else:
                fill, err = sim_buy(cd, mint, s["max_per_trade"], dec, now, sym, ev["sig"], s["max_slippage_pct"])
                cd["copied"][ev["sig"]] = now
                status = (f"🧪 COPY TRADE SIMULATED: bought {T.usd(fill['usd'])} at {T.px(fill['price'])} "
                          f"(quote impact {fill['impact']:.1f}%). No transaction sent.") if fill else "FAILED: " + err
                if err:
                    cd.setdefault("failed", []).append(dict(ts=now, sig=ev["sig"], sym=sym, why=err))
        else:  # sell
            post = (ev.get("post") or {}).get(mint, 0.0)
            full = post <= 1e-9
            if ev["sig"] in cd["copied"]:
                status = "duplicate: already handled"
            elif mint not in cd.get("positions", {}):
                status = "nothing to copy: no simulated position in this token"
            elif (full and not s["copy_full"]) or (not full and not s["copy_partial"]):
                status = f"not copied: copying {'full exits' if full else 'partial sells'} is off"
            else:
                frac = 1.0 if full else qty / (qty + post)
                fill, err = sim_sell(cd, mint, frac, dec or 6, now, ev["sig"])
                cd["copied"][ev["sig"]] = now
                status = (f"🧪 COPY SELL SIMULATED: sold {frac:.0%} for {T.usd(fill['usd'])} "
                          f"(P&L {T.usd(fill['pnl'])}). No transaction sent.") if fill else "FAILED: " + err
        self.audit(dict(ts=now, sig=ev["sig"], kind=ev["kind"], sym=sym, status=status))
        if self.gate.allow("copy", f"ct:{ev['sig']}", cooldown_min=0, now=now, settings=st) or state == "on":
            self.send(head + f"\n\n<b>Replication:</b> {e(status)}")
            out += 1
        return out

    def audit(self, row):
        a = self.data.setdefault("audit", [])
        a.append(row)
        self.data["audit"] = a[-500:]

    def process_decisions(self, now=None):
        """Apply ✅/❌ answers from the confirm buttons (written by the listener into copy.json)."""
        now = now or time.time()
        cfg = load_cfg()
        ct = cfg["copytrade"]
        cd = self.data.setdefault("copytrade", {})
        pend = cd.get("pending", {})
        changed = False
        for key, ans in list(ct.get("decisions", {}).items()):
            p = pend.pop(key, None)
            ct["decisions"].pop(key)
            changed = True
            if not p:
                continue
            if ans != "y":
                cd.setdefault("skipped", []).append(dict(ts=now, sig=p["sig"], sym=p["sym"], why=["you skipped it"]))
                continue
            if ct.get("state") != "on":
                self.send(f"Not copied: copy trading is {ct.get('state', 'off').upper()}.")
                continue
            b = T.collect(p["mint"], self.cache, deep=True, trades=False)
            a = T.analyze(b, [])
            ev = dict(ts=p["ev_ts"], sig=p["sig"])
            why = checks_buy(ct, cd, ev, p["mint"], a, now, p["src_price"])
            why = [w for w in why if "old (limit" not in w]  # you approved it; age was checked at detection
            if why:
                self.send(f"⏭ <b>COPY TRADE SKIPPED ${html.escape(p['sym'])}</b>\nConditions changed: " + html.escape("; ".join(why)))
                continue
            fill, err = sim_buy(cd, p["mint"], ct["settings"]["max_per_trade"], p["decimals"], now, p["sym"], p["sig"],
                                ct["settings"]["max_slippage_pct"])
            cd.setdefault("copied", {})[p["sig"]] = now
            self.send(f"🧪 <b>COPY TRADE SIMULATED ${html.escape(p['sym'])}</b>\n" +
                      (f"{T.usd(fill['usd'])} at {T.px(fill['price'])} · no transaction sent" if fill else "FAILED: " + err))
        for key in [k for k, p in pend.items() if now - p["ts"] > 300]:
            p = pend.pop(key)
            cd.setdefault("skipped", []).append(dict(ts=now, sig=p["sig"], sym=p["sym"], why=["confirmation timed out"]))
        if changed:
            save(p_cfg(), cfg)
        save(p_data(), self.data)


# ---------------------------------------------------------------- commands (listener)
def wallet_check(addr):
    """Validate + quick activity read for /copytrade set and /copywatch add."""
    if not P.valid_address(addr):
        return None, "That isn't a valid Solana address."
    kind = P.address_kind(addr)
    if kind is None:
        return None, "Couldn't reach Solana RPC to check the address. Try again in a minute."
    if kind != "wallet":
        return None, f"That address is a {kind.replace('_', ' ')}, not a wallet."
    sigs = rpc_fn("getSignaturesForAddress", [addr, {"limit": 100}])
    if sigs is None:
        return None, "Couldn't read the wallet's transactions (RPC busy). Try again."
    now = time.time()
    act = dict(n=len(sigs), d1=sum(1 for s in sigs if now - (s.get("blockTime") or 0) < 86400),
               d7=sum(1 for s in sigs if now - (s.get("blockTime") or 0) < 7 * 86400),
               last=sigs[0].get("blockTime") if sigs else None, failed=sum(1 for s in sigs if s.get("err")))
    return act, None


def activity_line(act):
    if not act["n"]:
        return "No transactions yet."
    return (f"Last activity {T.ago(time.time() - act['last'])} ago · {act['d1']} transactions in 24h · {act['d7']} in 7d"
            f"{' (100+ recent, very active)' if act['n'] >= 100 else ''}"
            + (" · actively trading" if act["d1"] >= 3 else " · quiet lately" if act["d7"] < 3 else ""))


def panel(cfg, data):
    ct = cfg["copytrade"]
    cd = data.get("copytrade", {})
    w = ct.get("wallet")
    wd = (data.get("wallets") or {}).get(w, {}) if w else {}
    st = ct.get("state", "off")
    icon = {"on": "🟢 ON", "off": "🔴 OFF", "paused": "⏸ PAUSED"}[st]
    fills = cd.get("fills", [])
    realized = sum(f.get("pnl", 0) for f in fills if f["side"] == "sell")
    last = cd.get("last_detected")
    L = [f"<b>COPY TRADING</b> (simulated execution)", f"Status: <b>{icon}</b>",
         f"Selected wallet: " + (f"<code>{w}</code>" if w else "none: /copytrade set &lt;address&gt;"),
         "Monitoring: " + ((f"yes, checked {T.ago(time.time() - wd['last_poll'])} ago" if wd.get("last_poll") else "starting")
                           if w else "no"),
         "Last detected: " + (f"{last['kind']} ${html.escape(last['sym'])} at {T.clock(last['ts'])}" if last else "none yet"),
         f"Trades detected: {cd.get('detected_n', 0)} · simulated copies: {len(fills)} · skipped: {len(cd.get('skipped', []))}",
         f"Session: simulated balance {T.usd(cd.get('cash', ct['settings']['bankroll']))} · realized {T.usd(realized)}",
         "<i>No private key is stored and no transaction is ever sent. Fills are real Jupiter quotes, recorded on paper.</i>"]
    buttons = [[dict(text="🟢 ENABLE COPY TRADING", callback_data="ct:on"),
                dict(text="🔴 DISABLE COPY TRADING", callback_data="ct:off")],
               [dict(text="🛑 EMERGENCY STOP", callback_data="ct:stop")]]
    return "\n".join(L), buttons


def settings_text(ct):
    s = ct["settings"]
    rows = "\n".join(f"{k} = {'on' if (k in BOOL_KEYS and s[k]) else 'off' if k in BOOL_KEYS else s[k]}" for k in SETTINGS)
    return ("<b>Copy-trade settings</b> (enforced by the bot before every simulated fill)\n<pre>" + rows + "</pre>\n"
            "Change: /copytrade settings max_per_trade 2 · /copytrade settings require_confirm on")


def turn_on_text(cfg, data):
    ct = cfg["copytrade"]
    s = ct["settings"]
    return (f"Turn ON copy trading (SIMULATED) for <code>{ct['wallet']}</code>?\n"
            f"Per trade {T.usd(s['max_per_trade'])} · daily {T.usd(s['max_daily'])} · allocation {T.usd(s['max_allocation'])} · "
            f"slippage ≤{s['max_slippage_pct']}% · price deviation ≤{s['max_deviation_pct']}% · risk ≤{s['max_risk']:.0f}\n"
            "Nothing is bought for real: each eligible trade is recorded at a real Jupiter quote."), \
        [[dict(text="✅ Confirm ON", callback_data="ct:confirm_on"), dict(text="Cancel", callback_data="ct:cancel")]]


def stats_text(cfg, data):
    ct = cfg["copytrade"]
    cd = data.get("copytrade", {})
    fills = cd.get("fills", [])
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    realized = sum(f.get("pnl", 0) for f in sells)
    pos = cd.get("positions", {})
    unreal, lines = 0.0, []
    if pos:
        r = P.ds_tokens(list(pos))
        for m, p in pos.items():
            bp = P.best_pair((r.get("data") or {}).get(m))
            if bp:
                val = p["qty"] * float(bp.get("priceUsd") or 0)
                unreal += val - p["cost"]
                lines.append(f"${html.escape(p['sym'])}: cost {T.usd(p['cost'])} → {T.usd(val)} (unrealized)")
            else:
                lines.append(f"${html.escape(p['sym'])}: cost {T.usd(p['cost'])} → no price now")
    start = ct.get("session_start")
    return "\n".join([
        "<b>Copy-trade stats</b> (all SIMULATED)",
        f"Source trades detected: {cd.get('detected_n', 0)}",
        f"Simulated copies: {len(buys)} buys, {len(sells)} sells · skipped {len(cd.get('skipped', []))} · failed {len(cd.get('failed', []))}",
        f"Spent: {T.usd(sum(f['usd'] for f in buys))}",
        f"<b>Realized</b> P&L: {T.usd(realized)}",
        f"<b>Unrealized</b> P&L: {T.usd(unreal)}" + (" (open positions below)" if lines else ""),
        *lines,
        "Fees: quotes include DEX fees and price impact; network and fomo fees aren't modeled (no real transactions).",
        f"Session: {T.ago(time.time() - start) if start else 'not started'}"] +
        [f"Last skips: " + "; ".join(f"${html.escape(x['sym'])}: {html.escape(x['why'][0])}" for x in cd.get("skipped", [])[-3:])]
        if cd.get("skipped") else [])


def cmd_copytrade(args, reply):
    """reply(text, buttons=None). Returns nothing."""
    cfg = load_cfg()
    data = load(p_data(), {})
    ct = cfg["copytrade"]
    sub = args[0].lower() if args else ""
    if sub in ("", "panel"):
        t, b = panel(cfg, data)
        return reply(t, b)
    if sub in ("set", "change"):
        if len(args) < 2:
            return reply(f"Usage: /copytrade {sub} &lt;wallet address&gt;")
        addr = args[1]
        reply("⏳ Checking that wallet…")
        act, err = wallet_check(addr)
        if err:
            return reply(err)
        ct["wallet"], ct["label"] = addr, (args[2] if len(args) > 2 else "copy wallet")
        if ct["state"] == "on":
            ct["state"] = "off"  # new wallet = re-confirm before copying
        save(p_cfg(), cfg)
        wd = (data.get("wallets") or {}).get(addr)
        prof = profile_text(ct["label"], addr, profile(wd, data.get("solpx", {}))) if wd else \
            "Trading history will load in the background over the next few minutes: /copytrade wallet to see it."
        return reply(f"✅ Saved <code>{addr}</code> as the copy-trade wallet.\n{activity_line(act)}\n\n{prof}\n\n"
                     "Copy trading is OFF. Review /copytrade settings, then /copytrade on to confirm.")
    if sub == "wallet":
        w = ct.get("wallet")
        if not w:
            return reply("No wallet selected. /copytrade set &lt;address&gt;")
        wd = (data.get("wallets") or {}).get(w)
        return reply(profile_text(ct.get("label") or "copy wallet", w, profile(wd, data.get("solpx", {})))
                     if wd else f"<code>{w}</code>: history still loading.")
    if sub == "remove":
        ct["wallet"], ct["state"] = None, "off"
        save(p_cfg(), cfg)
        return reply("Removed the copy-trade wallet and turned copy trading OFF.")
    if sub == "settings":
        if len(args) >= 3 and args[1] in SETTINGS:
            k, v = args[1], args[2].lower()
            if k in BOOL_KEYS:
                if v not in ("on", "off", "1", "0", "yes", "no"):
                    return reply("Use on or off.")
                ct["settings"][k] = 1 if v in ("on", "1", "yes") else 0
            else:
                try:
                    val = float(v)
                except ValueError:
                    return reply("Value must be a number.")
                if val < 0 or (k == "max_per_trade" and val < 2):
                    return reply("Value too low (fomo's minimum order is $2)." if k == "max_per_trade" else "Must be ≥ 0.")
                ct["settings"][k] = val
            save(p_cfg(), cfg)
            return reply(f"Set {k} = {args[2]}.\n\n" + settings_text(ct))
        return reply(settings_text(ct))
    if sub == "on":
        if not ct.get("wallet"):
            return reply("Select a wallet first: /copytrade set &lt;address&gt;")
        t, b = turn_on_text(cfg, data)
        return reply(t, b)
    if sub in ("off", "stop"):
        ct["state"] = "off"
        data_cd = data.setdefault("copytrade", {})
        n = len(data_cd.get("pending", {}))
        for k in list(data_cd.get("pending", {})):
            ct["decisions"][k] = "n"  # cancel anything waiting for confirmation
        save(p_cfg(), cfg)
        return reply(f"🛑 Copy trading OFF. {n} pending confirmation(s) cancelled. Monitoring continues; nothing will be copied.")
    if sub == "pause":
        if ct["state"] != "on":
            return reply("Copy trading isn't ON.")
        ct["state"] = "paused"
        save(p_cfg(), cfg)
        return reply("⏸ Paused. Settings and wallet kept; /copytrade resume to continue.")
    if sub == "resume":
        if ct["state"] != "paused":
            return reply("Not paused." + (" Use /copytrade on." if ct["state"] == "off" else ""))
        ct["state"] = "on"
        save(p_cfg(), cfg)
        return reply("▶️ Resumed (simulated execution).")
    if sub == "status":
        t, b = panel(cfg, data)
        return reply(t + "\n\n" + settings_text(ct), b)
    if sub == "stats":
        return reply(stats_text(cfg, data))
    return reply("Use /copytrade, set, change, wallet, remove, on, off, pause, resume, status, settings, stats.")


def on_button(data_str):
    """Handle an inline-button press. Returns a short text to show."""
    cfg = load_cfg()
    ct = cfg["copytrade"]
    if data_str.startswith("ctc:"):
        _, key, ans = data_str.split(":")
        ct["decisions"][key] = ans
        save(p_cfg(), cfg)
        return "Noted: " + ("copying (simulated)" if ans == "y" else "skipped")
    act = data_str.split(":", 1)[1]
    if act in ("off", "stop"):
        msgs = []
        cmd_copytrade(["off"], lambda t, b=None: msgs.append(t))
        return msgs[-1] if msgs else "OFF"
    if act == "on":
        if not ct.get("wallet"):
            return "Select a wallet first: /copytrade set <address>"
        return "CONFIRM"  # listener shows the confirm message
    if act == "confirm_on":
        if not ct.get("wallet"):
            return "No wallet selected."
        ct["state"] = "on"
        ct["ever_enabled"] = True
        ct["session_start"] = time.time()
        save(p_cfg(), cfg)
        return "🟢 Copy trading ON (simulated execution)."
    if act == "cancel":
        return "Cancelled. Still " + ct["state"].upper() + "."
    return "?"


def cmd_copywatch(args, reply):
    cfg = load_cfg()
    data = load(p_data(), {})
    tr = cfg["traders"]
    sub = args[0].lower() if args else ""
    if not sub:
        rows = []
        for t in tr.values():
            if not t["wallets"]:
                rows.append(f"⚪ <b>{html.escape(t['name'])}</b>: needs address (copy it from their fomo profile, then "
                            f"/copywatch add {html.escape(t['name'])} &lt;address&gt;)")
            for w in t["wallets"]:
                wd = (data.get("wallets") or {}).get(w["addr"], {})
                rows.append(f"🟢 <b>{html.escape(t['name'])}</b> <code>{w['addr']}</code>\n    {html.escape(w['source'])} · "
                            + (f"checked {T.ago(time.time() - wd['last_poll'])} ago · {len(wd.get('events', {}))} tx loaded"
                               if wd.get("last_poll") else "starting"))
        return reply("👥 <b>Copy-watch list</b> (alerts only, never buys)\n" + "\n".join(rows) +
                     "\n\nI can't read fomo profiles without signing in, so I never guess addresses.")
    if sub == "add":
        if len(args) == 2:
            name, addr = short(args[1]), args[1]
        elif len(args) >= 3:
            name, addr = args[1], args[2]
        else:
            return reply("Usage: /copywatch add &lt;username&gt; &lt;address&gt;")
        reply("⏳ Checking that wallet…")
        act, err = wallet_check(addr)
        if err:
            return reply(err)
        t = tr.setdefault(name.lower(), dict(name=name, wallets=[]))
        if any(w["addr"] == addr for w in t["wallets"]):
            return reply("Already tracked.")
        t["wallets"].append(dict(addr=addr, source="address supplied by you", added=time.time()))
        save(p_cfg(), cfg)
        return reply(f"✅ Tracking <b>{html.escape(t['name'])}</b> <code>{addr}</code>\n{activity_line(act)}\n"
                     "On-chain this is an active wallet; I can't confirm it belongs to that fomo user.\n"
                     "History loads in the background; /copywatch stats " + html.escape(t["name"]) + " in a few minutes.")
    if sub == "remove" and len(args) >= 2:
        key = args[1]
        for n, t in list(tr.items()):
            if n == key.lower():
                t["wallets"] = []
                save(p_cfg(), cfg)
                return reply(f"Removed {html.escape(t['name'])}'s wallets.")
            for w in list(t["wallets"]):
                if w["addr"] == key:
                    t["wallets"].remove(w)
                    save(p_cfg(), cfg)
                    return reply(f"Removed {key[:6]}… from {html.escape(t['name'])}.")
        return reply("Not found.")
    if sub in ("stats", "activity") and len(args) >= 2:
        t = tr.get(args[1].lower())
        if not t or not t["wallets"]:
            return reply("No address for that trader yet.")
        out = []
        for w in t["wallets"]:
            wd = (data.get("wallets") or {}).get(w["addr"])
            if not wd:
                out.append(f"<code>{w['addr']}</code>: history not loaded yet.")
                continue
            if sub == "stats":
                evs = wd.get("events", {})
                mints = list(dict.fromkeys(m for e in evs.values() for m in (e.get("tokens") or {})))[:60]
                r = P.ds_tokens(mints)
                mkt = {m: T.pair_metrics(P.best_pair(ps), time.time()) for m, ps in (r.get("data") or {}).items() if ps}
                out.append(profile_text(t["name"], w["addr"], profile(wd, data.get("solpx", {}), mkt)))
            else:
                evs = sorted(wd.get("events", {}).values(), key=lambda e: -e["ts"])[:12]
                lines = []
                for e in evs:
                    tok = next(iter(e.get("tokens") or {}), None)
                    lines.append(f"{T.when(e['ts'])} {e.get('kind')} " + (f"<code>{short(tok)}</code>" if tok else "")
                                 + f" <a href=\"{solscan_tx(e['sig'])}\">tx</a>")
                out.append(f"<b>{html.escape(t['name'])}</b> recent activity:\n" + ("\n".join(lines) or "none loaded"))
        return reply("\n\n".join(out))
    return reply("Use /copywatch, add &lt;name&gt; &lt;address&gt;, remove &lt;name|address&gt;, stats &lt;name&gt;, activity &lt;name&gt;")
