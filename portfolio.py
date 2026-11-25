"""Portfolio accounting for /stats, built only from the wallet's own on-chain history.

Every number comes from a real transaction or a live balance. SOL legs are valued with the hourly
SOL/USD price at the time of the trade (GeckoTerminal). Anything that can't be verified (history not
fully loaded, a trade that couldn't be priced, fomo's own fee inside swap prices) is reported as missing,
never filled in.
"""
import html
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    TZ = timezone.utc

ATA_RENT = 0.00203928  # refundable rent for a token account: not a cost, not income
SOL_POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"  # Orca SOL/USDC, deepest SOL pool on DexScreener
DUST_USD = 0.05


def esc(s):
    return html.escape(str(s))


def money(x, signed=False):
    if x is None:
        return "n/a"
    s = f"${abs(x):,.2f}"
    if x < 0:
        return "-" + s
    return ("+" + s) if signed else s


def day_of(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- history sync
def sync(pf, owner, rpc, stables, budget=40):
    """Pull new signatures and backfill older ones, then parse up to budget transactions per call.
    Raw parsed events are stored in pf['events'] keyed by signature."""
    pf.setdefault("events", {})
    pf.setdefault("pending", [])
    pf.setdefault("complete", False)
    # newest first
    opts = {"limit": 100}
    if pf.get("newest"):
        opts["until"] = pf["newest"]
    new = rpc("getSignaturesForAddress", [owner, opts])
    if new is not None:
        if new:
            pf["newest"] = new[0]["signature"]
            if not pf.get("oldest"):
                pf["oldest"] = new[-1]["signature"]
        pf["pending"] = [s["signature"] for s in new if not s.get("err")] + pf["pending"]
    # backfill older history while the queue is short
    if not pf["complete"] and pf.get("oldest") and len(pf["pending"]) < 50:
        old = rpc("getSignaturesForAddress", [owner, {"limit": 100, "before": pf["oldest"]}])
        if old is not None:
            if old:
                pf["oldest"] = old[-1]["signature"]
                pf["pending"] += [s["signature"] for s in old if not s.get("err")]
            if len(old) < 100:
                pf["complete"] = True
    if new is not None and not new and not pf.get("newest"):
        pf["complete"] = True  # brand-new wallet with no history
    done = 0
    while pf["pending"] and done < budget:
        sig = pf["pending"].pop(0)
        if sig in pf["events"]:
            continue
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        done += 1
        if tx is None:
            pf["pending"].append(sig)  # retry later
            continue
        ev = parse_tx(tx, owner, stables)
        pf["events"][sig] = ev or {"ts": tx.get("blockTime") or 0, "skip": True}
    return done


def parse_tx(tx, owner, stables, wsol="So11111111111111111111111111111111111111112"):
    """Net effect of one transaction on owner: token deltas, stablecoin delta, SOL delta (rent and network
    fee separated out). Returns None for failed transactions."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    pre, post = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bal(lst, m):
        return sum(float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for b in lst if b.get("owner") == owner and b.get("mint") == m)

    mints = {b.get("mint") for b in pre + post if b.get("owner") == owner}
    tokens = {}
    for m in mints:
        if m in stables or m == wsol:
            continue
        d = bal(post, m) - bal(pre, m)
        if abs(d) > 0:
            tokens[m] = d
    stable = sum(bal(post, q) - bal(pre, q) for q in stables)
    sol, fee = 0.0, 0.0
    if owner in keys:
        i = keys.index(owner)
        try:
            sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        except (KeyError, IndexError, TypeError):
            sol = 0.0
        if keys and keys[0] == owner:  # owner paid the network fee
            fee = (meta.get("fee") or 0) / 1e9
    pre_m = {b.get("mint") for b in pre if b.get("owner") == owner}
    post_m = {b.get("mint") for b in post if b.get("owner") == owner}
    opened = len([m for m in post_m - pre_m if m != wsol])
    closed = len([m for m in pre_m - post_m if m != wsol])
    # SOL leg of the trade = change in SOL + wrapped SOL, with the fee and refundable rent taken out
    sol_leg = sol + fee + opened * ATA_RENT - closed * ATA_RENT + (bal(post, wsol) - bal(pre, wsol))
    if abs(sol_leg) < 1e-6:
        sol_leg = 0.0
    return dict(ts=tx.get("blockTime") or 0, tokens=tokens, stable=stable, sol=sol_leg, fee=fee)


def classify(ev):
    if ev.get("skip"):
        return "skip"
    t = ev["tokens"]
    ins = [m for m, d in t.items() if d > 0]
    outs = [m for m, d in t.items() if d < 0]
    paid = ev["stable"] < -0.005 or ev["sol"] < -1e-5
    got = ev["stable"] > 0.005 or ev["sol"] > 1e-5
    if ins and not outs:
        return "buy" if paid else "transfer_in"
    if outs and not ins:
        return "sell" if got else "transfer_out"
    if ins and outs:
        return "swap"
    if got and not paid:
        return "deposit"
    if paid and not got:
        return "withdraw"
    return "fee_only" if ev.get("fee") else "other"


# ---------------------------------------------------------------- SOL prices
def ensure_sol_prices(pf, need_ts, fetch):
    """Hourly SOL/USD closes covering the oldest event. fetch(url) returns GeckoTerminal JSON."""
    hp = pf.setdefault("solpx", {})
    if not need_ts:
        return
    oldest_needed = min(need_ts) // 3600 * 3600
    newest_needed = max(need_ts) // 3600 * 3600
    have = sorted(int(k) for k in hp)
    calls = 0
    if not have or have[-1] < newest_needed:
        data = fetch(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{SOL_POOL}/ohlcv/hour"
                     f"?limit=1000&token=So11111111111111111111111111111111111111112")
        calls += 1
        for row in ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list", []) or []:
            hp[str(int(row[0]))] = row[4]
        have = sorted(int(k) for k in hp)
    while have and have[0] > oldest_needed and calls < 3:
        data = fetch(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{SOL_POOL}/ohlcv/hour"
                     f"?limit=1000&before_timestamp={have[0]}&token=So11111111111111111111111111111111111111112")
        calls += 1
        rows = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list", []) or []
        if not rows:
            break
        for row in rows:
            hp[str(int(row[0]))] = row[4]
        have = sorted(int(k) for k in hp)


def sol_at(pf, ts):
    hp = pf.get("solpx") or {}
    h = int(ts) // 3600 * 3600
    for k in (h, h - 3600, h + 3600):  # nearest hour
        if str(k) in hp:
            return hp[str(k)]
    return None


# ---------------------------------------------------------------- ledger
def ledger(pf):
    """Average-cost ledger over all parsed events. Returns totals, per-coin lots, closed trades, per-day stats."""
    evs = sorted((e for e in pf.get("events", {}).values() if not e.get("skip")), key=lambda e: e["ts"])
    lots, trades, days = {}, [], {}
    tot = dict(deposits=0.0, withdrawals=0.0, invested=0.0, proceeds=0.0, fees=0.0, buys=0, sells=0,
               unpriced=0, unknown_basis=0, transfers_in=0, swaps=0)

    def dd(ts):
        return days.setdefault(day_of(ts), dict(realized=0.0, volume=0.0, deposits=0.0, withdrawals=0.0, fees=0.0))

    for e in evs:
        kind = classify(e)
        p = sol_at(pf, e["ts"])
        usd_leg = e["stable"] + (e["sol"] * p if p is not None else (0 if e["sol"] == 0 else float("nan")))
        if e["fee"]:
            fee_usd = e["fee"] * p if p is not None else None
            if fee_usd is not None:
                tot["fees"] += fee_usd
                dd(e["ts"])["fees"] += fee_usd
        if usd_leg != usd_leg:  # NaN: SOL price unknown for this hour
            if kind in ("buy", "sell", "deposit", "withdraw"):
                tot["unpriced"] += 1
            continue
        if kind == "deposit":
            tot["deposits"] += usd_leg
            dd(e["ts"])["deposits"] += usd_leg
        elif kind == "withdraw":
            tot["withdrawals"] += -usd_leg
            dd(e["ts"])["withdrawals"] += -usd_leg
        elif kind == "buy":
            cost = -usd_leg
            n = len([m for m, d in e["tokens"].items() if d > 0])
            for m, d in e["tokens"].items():
                lot = lots.setdefault(m, dict(qty=0.0, cost=0.0, opened=e["ts"], free=0.0))
                if lot["qty"] <= 1e-12:
                    lot["opened"] = e["ts"]
                lot["qty"] += d
                lot["cost"] += cost / n
            tot["invested"] += cost
            tot["buys"] += 1
            dd(e["ts"])["volume"] += cost
            trades.append(dict(kind="buy", mint=next(iter(e["tokens"])), ts=e["ts"], usd=cost,
                               price=cost / max(sum(d for d in e["tokens"].values() if d > 0), 1e-18)))
        elif kind == "sell":
            proceeds = usd_leg
            (m, d), = [(m, d) for m, d in e["tokens"].items() if d < 0][:1] or [(None, 0)]
            sold = -d
            lot = lots.get(m)
            if lot and lot["qty"] >= sold * 0.999:
                basis = lot["cost"] * sold / lot["qty"]
                lot["cost"] -= basis
                lot["qty"] -= sold
                pnl = proceeds - basis
                trades.append(dict(kind="sell", mint=m, ts=e["ts"], usd=proceeds, basis=basis, pnl=pnl,
                                   pct=(pnl / basis * 100) if basis else None, held=e["ts"] - lot["opened"],
                                   price=proceeds / sold if sold else None))
                dd(e["ts"])["realized"] += pnl
            else:
                tot["unknown_basis"] += 1
                trades.append(dict(kind="sell", mint=m, ts=e["ts"], usd=proceeds, basis=None, pnl=None, pct=None,
                                   held=None, price=proceeds / sold if sold else None))
            tot["proceeds"] += proceeds
            tot["sells"] += 1
            dd(e["ts"])["volume"] += proceeds
        elif kind == "transfer_in":
            for m, d in e["tokens"].items():
                lot = lots.setdefault(m, dict(qty=0.0, cost=0.0, opened=e["ts"], free=0.0))
                lot["qty"] += d
                lot["free"] += d
            tot["transfers_in"] += 1
        elif kind == "transfer_out":
            for m, d in e["tokens"].items():
                lot = lots.get(m)
                if lot and lot["qty"] > 0:
                    share = min(1.0, -d / lot["qty"])
                    lot["cost"] -= lot["cost"] * share
                    lot["qty"] += d
        elif kind == "swap":
            tot["swaps"] += 1
    return dict(totals=tot, lots=lots, trades=trades, days=days, n_events=len(evs))


# ---------------------------------------------------------------- /stats report
def period_rows(L, now):
    today = day_of(now)
    rows = []
    for label, n in (("Today", 1), ("7 days", 7), ("30 days", 30), ("All", None)):
        cut = None if n is None else (datetime.fromtimestamp(now, TZ) - timedelta(days=n - 1)).strftime("%Y-%m-%d")
        sel = [v for k, v in L["days"].items() if cut is None or k >= cut]
        if n == 1:
            sel = [L["days"][today]] if today in L["days"] else []
        agg = {k: sum(s[k] for s in sel) for k in ("realized", "volume", "deposits", "withdrawals", "fees")}
        rows.append((label, agg))
    return rows


def build_stats(pf, L, wallet_now, holdings, intel, now, stamp, calls_text=""):
    """wallet_now: dict(stable, sol, sol_usd, ok). holdings: list of dict(mint, sym, qty, price, value, liq, mcap,
    ch_h1, ch_h24). intel: {mint: analysis or None}. Returns list of Telegram messages."""
    t = L["totals"]
    sells = [x for x in L["trades"] if x["kind"] == "sell" and x["pnl"] is not None]
    wins = [x for x in sells if x["pnl"] > 0]
    losses = [x for x in sells if x["pnl"] <= 0]
    coins_value = sum(h["value"] for h in holdings)
    balance = (wallet_now.get("stable", 0) + wallet_now.get("sol_usd", 0) + coins_value) if wallet_now.get("ok") else None
    unreal_p = unreal_l = 0.0
    hold_rows = []
    for h in holdings:
        lot = L["lots"].get(h["mint"])
        matched = lot and lot["qty"] > 0 and abs(lot["qty"] - h["qty"]) <= max(1e-9, 0.01 * h["qty"])
        avg = (lot["cost"] / lot["qty"]) if matched and lot["qty"] else None
        invested = avg * h["qty"] if avg is not None else None
        un = h["value"] - invested if invested is not None else None
        if un is not None:
            if un >= 0:
                unreal_p += un
            else:
                unreal_l += un
        realized = sum(x["pnl"] for x in sells if x["mint"] == h["mint"])
        hold_rows.append(dict(h, avg=avg, invested=invested, unreal=un, realized=realized, matched=bool(matched),
                              lot_qty=lot["qty"] if lot else 0.0))
    real_p = sum(x["pnl"] for x in wins)
    real_l = sum(x["pnl"] for x in losses)
    net = real_p + real_l + unreal_p + unreal_l
    base = t["deposits"] or t["invested"]
    ret = (net / base * 100) if base else None
    g = lambda x: "🟢" if (x or 0) > 0 else "🔴" if (x or 0) < 0 else "⚪"

    m1 = [f"📒 <b>Your FOMO portfolio</b> · data {stamp}"]
    if balance is not None:
        m1.append(f"Balance now: <b>{money(balance)}</b> (stablecoins {money(wallet_now['stable'])} · SOL "
                  f"{money(wallet_now['sol_usd'])} · coins {money(coins_value)}) ✅ live from your wallet")
    else:
        m1.append("Balance now: couldn't read your wallet this run (free data server hiccup).")
    m1 += [f"Deposited {money(t['deposits'])} · Withdrawn {money(t['withdrawals'])} · Total bought {money(t['invested'])}",
           f"Realized: 🟢 {money(real_p, True)} profit · 🔴 {money(real_l)} loss",
           f"Unrealized: 🟢 {money(unreal_p, True)} · 🔴 {money(unreal_l)}",
           f"{g(net)} <b>Net P&amp;L {money(net, True)}</b>" + (f" ({ret:+.1f}% of {'deposits' if t['deposits'] else 'money invested'})" if ret is not None else ""),
           f"Trades: {t['buys']} buys + {t['sells']} sells = {t['buys'] + t['sells']}"]
    if sells:
        wr = len(wins) / len(sells) * 100
        best = max(sells, key=lambda x: x["pnl"])
        worst = min(sells, key=lambda x: x["pnl"])
        sym = lambda mt: next((h["sym"] for h in holdings if h["mint"] == mt), mt[:4] + "…")
        m1 += [f"Closed: {len(wins)} wins / {len(losses)} losses · win rate {wr:.0f}%",
               f"Avg win {money(sum(x['pnl'] for x in wins) / len(wins), True) if wins else 'n/a'} · "
               f"Avg loss {money(sum(x['pnl'] for x in losses) / len(losses)) if losses else 'n/a'}",
               f"Largest win: {esc(sym(best['mint']))} {money(best['pnl'], True)} · "
               f"Largest loss: {esc(sym(worst['mint']))} {money(worst['pnl'])}"]
    else:
        m1.append("No closed trades yet, so no win rate.")
    m1.append(f"Network fees paid: {money(t['fees'])}. fomo's own trading fee is built into swap prices and "
              "isn't reported separately, so it's already inside the P&amp;L above.")
    rows = period_rows(L, now)
    tbl = ["Period   Realized  Volume   In/Out", "-------  --------  -------  ------------"]
    for label, a in rows:
        tbl.append(f"{label:<8} {money(a['realized'], True):>8}  {money(a['volume']):>7}  "
                   f"{money(a['deposits'])}/{money(a['withdrawals'])}")
    m1.append("<pre>" + esc("\n".join(tbl)) + "</pre>")
    if L["days"]:
        best_d = max(L["days"].items(), key=lambda kv: kv[1]["realized"])
        worst_d = min(L["days"].items(), key=lambda kv: kv[1]["realized"])
        m1.append(f"Best day {best_d[0]} {money(best_d[1]['realized'], True)} · "
                  f"worst day {worst_d[0]} {money(worst_d[1]['realized'])} (realized)")

    # ---- holdings
    m2 = ["💼 <b>Holdings</b>"]
    if not hold_rows:
        m2.append("No coins held right now.")
    for h in sorted(hold_rows, key=lambda h: -h["value"]):
        a = intel.get(h["mint"])
        ret_h = (h["unreal"] / h["invested"] * 100) if h["invested"] else None
        status = ("Positive momentum" if a and a["momentum"] == "Strong" else
                  "Weakening" if a and a["momentum"] in ("Weak", "Weakening") else
                  "Steady" if a else "unknown")
        risk = a["risk"] if a else "unknown"
        m2.append(f"{g(h['unreal'])} <b>${esc(h['sym'])}</b>")
        m2.append(f"Qty {h['qty']:,.0f} · avg buy {h['avg']:.10g} · now {h['price']:.10g}" if h["avg"] is not None
                  else f"Qty {h['qty']:,.0f} · now {h['price']:.10g} · avg buy unknown")
        if h["invested"] is not None:
            rtxt = f" ({ret_h:+.0f}%)" if ret_h is not None else " (received free: airdrop or transfer)"
            m2.append(f"Invested {money(h['invested'])} → value {money(h['value'])} · unrealized {money(h['unreal'], True)}"
                      f"{rtxt} · realized {money(h['realized'], True)}")
        else:
            m2.append(f"Value {money(h['value'])} · invested unknown: the bot's history shows {h['lot_qty']:,.0f} "
                      f"tokens vs {h['qty']:,.0f} in your wallet (history still loading or a transfer)")
        m2.append(f"Liquidity {money(h['liq'])} · MC {money(h['mcap'])} · 1h {h['ch_h1']:+.0f}% · 24h {h['ch_h24']:+.0f}%")
        m2.append(f"Status {status} · Risk {risk}" + (f": {esc(a['risk_reasons'][0])}" if a else ""))

    # ---- behavior, risk, alerts, data notes
    m3 = ["🧠 <b>Trading behavior</b>"]
    if len(sells) < 5:
        m3.append(f"Only {len(sells)} closed trade(s) with a known cost. Patterns need at least 5, so none are claimed yet.")
    else:
        stop_breach = [x for x in losses if x["pct"] is not None and x["pct"] < -18]
        m3.append(f"Holding losers too long: {len(stop_breach)} of {len(losses)} losses were sold below the −18% stop"
                  + (f" (average {sum(x['pct'] for x in stop_breach) / len(stop_breach):.0f}%)" if stop_breach else ""))
        hw = [x["held"] for x in wins if x["held"]]
        hl = [x["held"] for x in losses if x["held"]]
        if hw and hl:
            m3.append(f"Average hold: winners {sum(hw) / len(hw) / 3600:.1f}h vs losers {sum(hl) / len(hl) / 3600:.1f}h"
                      + (" → you hold losers longer than winners" if sum(hl) / len(hl) > 1.5 * sum(hw) / len(hw) else ""))
        by_coin = {}
        for x in sells:
            by_coin[x["mint"]] = by_coin.get(x["mint"], 0) + x["pnl"]
        if by_coin:
            sym = lambda mt: next((h["sym"] for h in holdings if h["mint"] == mt), mt[:4] + "…")
            top_c = max(by_coin.items(), key=lambda kv: kv[1])
            low_c = min(by_coin.items(), key=lambda kv: kv[1])
            m3.append(f"Most profit: {esc(sym(top_c[0]))} {money(top_c[1], True)} · most loss: "
                      f"{esc(sym(low_c[0]))} {money(low_c[1])}")
    chase = pf.get("chase") or []
    if len(chase) >= 3:
        hot = [c for c in chase if c >= 30]
        m3.append(f"Buying after pumps: {len(hot)} of {len(chase)} buys the bot could check came after a 30%+ rise "
                  "in the prior hour")
    else:
        m3.append("Buying after pumps: not enough buys with known prior-hour prices to judge yet.")

    m3.append("")
    m3.append("⚖️ <b>Portfolio risk</b>")
    if coins_value > 0:
        alloc = sorted(((h["value"] / coins_value * 100, h) for h in hold_rows), key=lambda x: -x[0])
        m3.append("Allocation: " + " · ".join(f"${esc(h['sym'])} {p:.0f}%" for p, h in alloc))
        low_liq = sum(h["value"] for h in hold_rows if h["liq"] < 20000) / coins_value * 100
        hi_risk = sum(h["value"] for h in hold_rows if (intel.get(h["mint"]) or {}).get("risk") in ("High", "Critical")) / coins_value * 100
        volat = [h for h in hold_rows if (intel.get(h["mint"]) or {}).get("sigma", (None,))[0] and
                 intel[h["mint"]]["sigma"][0] > 0.10]
        susp = [h for h in hold_rows if (intel.get(h["mint"]) or {}).get("risk") == "Critical"]
        falling = [h for h in hold_rows if ((intel.get(h["mint"]) or {}).get("liq_chg") or 0) <= -15]
        dist = [h for h in hold_rows if ((intel.get(h["mint"]) or {}).get("whales") or {}).get("status") == "Distributing"]
        nm = lambda xs: ", ".join("$" + esc(h["sym"]) for h in xs) or "none"
        top_share = alloc[0][0]
        share_total = coins_value / balance * 100 if balance else None
        m3 += [f"Largest position {top_share:.0f}% of your coins" + (f" · coins are {share_total:.0f}% of your balance" if share_total is not None else ""),
               f"In low-liquidity coins (&lt;$20k): {low_liq:.0f}% · in high/critical-risk coins: {hi_risk:.0f}%",
               f"Very volatile (1h σ &gt;10%): {nm(volat)} · Suspicious activity: {nm(susp)}",
               f"Liquidity declining: {nm(falling)} · Whales distributing: {nm(dist)}"]
        level = ("Critical" if susp else "High" if (top_share >= 60 or low_liq >= 50 or hi_risk >= 50) else
                 "Moderate" if (top_share >= 40 or low_liq >= 25 or hi_risk >= 25 or dist or falling) else "Low")
        m3.append(f"Overall portfolio risk: <b>{level}</b>")
        watch = susp + falling + dist
        if watch:
            m3.append("Watch closely: " + nm(list({h["mint"]: h for h in watch}.values())))
    else:
        m3.append("No coins held, so no coin risk. Cash only.")

    alerts = [a for a in pf.get("alerts", []) if now - a["ts"] <= 86400]
    m3.append("")
    m3.append("🚨 <b>Alerts on your coins (24h)</b>")
    m3 += [f"• {datetime.fromtimestamp(a['ts'], TZ).strftime('%I:%M %p')} ${esc(a['sym'])}: {esc(a['text'])}"
           for a in alerts[-8:]] or ["• None"]

    notes = []
    if not pf.get("complete"):
        notes.append(f"history still loading ({L['n_events']} transactions so far): totals above are partial")
    if t["unpriced"]:
        notes.append(f"{t['unpriced']} trade(s) couldn't be priced (no SOL price for that hour) and are left out")
    if t["unknown_basis"]:
        notes.append(f"{t['unknown_basis']} sell(s) of coins bought before the loaded history: profit unknown")
    mism = [h for h in hold_rows if not h["matched"]]
    if mism:
        notes.append("history doesn't match wallet balance for " + ", ".join("$" + esc(h["sym"]) for h in mism))
    else:
        notes.append("✅ ledger matches your wallet's coin balances")
    if t["swaps"]:
        notes.append(f"{t['swaps']} coin-to-coin swap(s) not counted (no dollar leg)")
    dv = pf.get("daily_value") or {}
    if dv:
        notes.append(f"portfolio value tracked daily since {min(dv)}; earlier daily values aren't available")
    m3.append("")
    m3.append("ℹ️ <b>Data</b>: " + "; ".join(notes) + ".")
    if calls_text:
        m3.append("Bot call grading moved to /calls.")
    return ["\n".join(m1), "\n".join(m2), "\n".join(m3)]
