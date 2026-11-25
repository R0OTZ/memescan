"""Market intelligence for /summary.

Pure analysis: takes data the scanner already fetched (DexScreener metrics, fomo's public feed,
RugCheck, GeckoTerminal large trades, and the bot's own price/holder history) and turns it into
indicators, a risk level, whale activity, scenarios and a category. Nothing here trades, and nothing
is guessed: a signal with no data behind it is reported as unavailable.
"""
import html
import math

RISK_ORDER = {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}
CATEGORIES = ("Strong Research Candidate", "Potential Momentum Opportunity", "High-Risk Speculation",
              "Weak Momentum", "Avoid / Critical Warning")
NOT_AVAILABLE = ("social engagement (X/Telegram activity needs a paid API)",
                 "influencer or named-investor wallets (no verifiable public mapping)",
                 "wallets' historical win rates and trading-group links")


def esc(s):
    return html.escape(str(s))


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else str(addr)


def pct(x, signed=True):
    return f"{x:+.0f}%" if signed else f"{x:.0f}%"


def usd(x):
    x = x or 0
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    if abs(x) >= 1e5:
        return f"${x / 1e3:.0f}k"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:,.2f}" if abs(x) < 100 else f"${x:,.0f}"


def ago(sec):
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec / 3600:.1f}h ago"
    return f"{sec / 86400:.1f}d ago"


# ---------------------------------------------------------------- building blocks
def volatility(px_hist, now):
    """1-hour standard deviation of returns from the bot's own price samples. None if too little data."""
    h = [x for x in px_hist if now - x[0] <= 6 * 3600 and x[1] > 0]
    if len(h) < 20:
        return None, len(h), 0
    rets, dts = [], []
    for a, b in zip(h, h[1:]):
        dt = b[0] - a[0]
        if dt > 0:
            rets.append(math.log(b[1] / a[1]))
            dts.append(dt)
    if len(rets) < 15:
        return None, len(h), 0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    avg_dt = sum(dts) / len(dts)
    sigma_1h = math.sqrt(var) * math.sqrt(3600 / avg_dt)
    return sigma_1h, len(h), (h[-1][0] - h[0][0]) / 3600


def levels(px_hist, now, price):
    recent = [x for x in px_hist if now - x[0] <= 7200]
    if len(recent) < 5:
        return None
    hi, lo = max(x[1] for x in recent), min(x[1] for x in recent)
    liq_then = [x[2] for x in px_hist if 3000 <= now - x[0] <= 4500]
    return dict(hi=hi, lo=lo, liq_1h_ago=(sum(liq_then) / len(liq_then)) if liq_then else None,
                span_h=(now - recent[0][0]) / 3600)


def whale_activity(trades, snaps, liq, now, top_owner_pct):
    """Large trades (GeckoTerminal, last 24h) + changes in top-holder balances (bot snapshots)."""
    out = dict(status="Unknown", wallets=[], new_wallets=None, buy_usd=0.0, sell_usd=0.0,
               n_buyers=0, n_sellers=0, holder_moves=[], wash=[], threshold=0.0, has_trades=trades is not None)
    if trades is not None:
        thr = max(250.0, (liq or 0) * 0.005)
        out["threshold"] = thr
        last1 = [t for t in trades if now - t["ts"] <= 3600]
        earlier = {t["wallet"] for t in trades if now - t["ts"] > 3600}
        big1 = [t for t in last1 if t["usd"] >= thr]
        buyers = {t["wallet"] for t in big1 if t["kind"] == "buy"}
        sellers = {t["wallet"] for t in big1 if t["kind"] == "sell"}
        out.update(buy_usd=sum(t["usd"] for t in big1 if t["kind"] == "buy"),
                   sell_usd=sum(t["usd"] for t in big1 if t["kind"] == "sell"),
                   n_buyers=len(buyers), n_sellers=len(sellers))
        active1 = {t["wallet"] for t in last1}
        if active1:
            out["new_wallets"] = (len(active1 - earlier), len(active1))
        big6 = sorted((t for t in trades if now - t["ts"] <= 6 * 3600 and t["usd"] >= thr),
                      key=lambda t: -t["usd"])[:5]
        out["wallets"] = [dict(t, pct=top_owner_pct.get(t["wallet"])) for t in big6]
        flips = {}
        for t in last1:
            flips.setdefault(t["wallet"], set()).add(t["kind"])
        counts = {}
        for t in last1:
            counts[t["wallet"]] = counts.get(t["wallet"], 0) + 1
        out["wash"] = [w for w, k in flips.items() if len(k) == 2 and counts[w] >= 6]
    # top-holder balance changes between bot snapshots (needs 30+ minutes of history)
    if snaps and len(snaps) >= 2:
        latest = snaps[-1]
        olds = [s for s in snaps if latest[0] - s[0] >= 1800]
        if olds:
            then = min(olds, key=lambda s: abs((latest[0] - s[0]) - 3600))
            for owner in set(latest[1]) | set(then[1]):
                d = latest[1].get(owner, 0.0) - then[1].get(owner, 0.0)
                if abs(d) >= 0.25:
                    out["holder_moves"].append((owner, d, latest[1].get(owner, 0.0), latest[0] - then[0]))
            out["holder_moves"].sort(key=lambda x: x[1])
    acc = out["buy_usd"] >= 1.5 * max(out["sell_usd"], 1) and out["n_buyers"] >= 2
    dist = out["sell_usd"] >= 1.5 * max(out["buy_usd"], 1) and out["n_sellers"] >= 2
    net_holders = sum(d for _, d, _, _ in out["holder_moves"])
    if acc or (net_holders >= 1.0 and not dist):
        out["status"] = "Accumulating"
    elif dist or net_holders <= -1.0:
        out["status"] = "Distributing"
    elif trades is not None:
        out["status"] = "Mixed" if (out["buy_usd"] + out["sell_usd"]) > 0 else "Quiet"
    return out


# ---------------------------------------------------------------- one coin
def analyze(c, now):
    """c: dict(mint, m, fm, rk, px, snaps, trades, holder_growth). Returns the full assessment."""
    m, fm, rk = c["m"], c.get("fm") or {}, c.get("rk")
    price, liq, mc = m["price"], m["liq"], m["mcap"] or 0
    tot1 = m["b1"] + m["s1"]
    br1 = m["b1"] / tot1 if tot1 else 0.5
    tot24 = m.get("b24", 0) + m.get("s24", 0)
    br24 = m.get("b24", 0) / tot24 if tot24 else None
    a1 = (m.get("vol_m5", 0) * 12 / m["vol_h1"]) if m["vol_h1"] else 0  # last 5 min vs hourly pace
    a6 = (m["vol_h1"] * 6 / m["vol_h6"]) if m.get("vol_h6") else None    # last hour vs 6h pace
    unusual = m.get("vol_h24") and m["vol_h1"] >= 3 * m["vol_h24"] / 24
    tfs = [m["ch_m5"], m["ch_h1"], m["ch_h6"], m.get("ch_h24", 0)]
    up_tfs = sum(1 for x in tfs if x > 0)
    lv = levels(c.get("px") or [], now, price)
    liq_chg = ((liq / lv["liq_1h_ago"] - 1) * 100) if lv and lv["liq_1h_ago"] else None
    g = c.get("holder_growth")  # per hour, from fomo holder counts
    top = (rk or {}).get("top") or []
    top_pct = {o: p for o, p in top}
    top10 = sum(p for _, p in top[:10])
    whales = whale_activity(c.get("trades"), c.get("snaps"), liq, now, top_pct)
    age_h = (m["age_min"] / 60) if m.get("age_min") is not None else None

    bull, bear = [], []
    if a6 is not None and a6 >= 1.3:
        bull.append(f"Volume rising: last hour {usd(m['vol_h1'])} is {a6:.1f}x its 6h hourly average")
    if a6 is not None and a6 <= 0.6:
        bear.append(f"Volume fading: last hour {usd(m['vol_h1'])} is only {a6:.1f}x its 6h hourly average")
    if br1 >= 0.55 and tot1 >= 40:
        bull.append(f"Buying pressure: {br1:.0%} of last-hour trades were buys ({m['b1']} buys / {m['s1']} sells)")
    if br1 <= 0.42 and tot1 >= 40:
        bear.append(f"Selling pressure: only {br1:.0%} of last-hour trades were buys ({m['b1']}/{m['s1']})")
    if up_tfs >= 3:
        bull.append(f"Up on {up_tfs}/4 timeframes (5m {pct(tfs[0])}, 1h {pct(tfs[1])}, 6h {pct(tfs[2])}, 24h {pct(tfs[3])})")
    elif up_tfs <= 1:
        bear.append(f"Down on {4 - up_tfs}/4 timeframes (5m {pct(tfs[0])}, 1h {pct(tfs[1])}, 6h {pct(tfs[2])}, 24h {pct(tfs[3])})")
    if g is not None and g >= 0.01:
        bull.append(f"Holders growing {g * 100:+.1f}%/h (fomo holder count)")
    if g is not None and g <= -0.005:
        bear.append(f"Holders shrinking {g * 100:+.1f}%/h (fomo holder count)")
    if liq_chg is not None and liq_chg >= 10:
        bull.append(f"Liquidity growing: {liq_chg:+.0f}% in the last hour (now {usd(liq)})")
    if liq_chg is not None and liq_chg <= -15:
        bear.append(f"Liquidity withdrawn: {liq_chg:+.0f}% in the last hour (now {usd(liq)})")
    if whales["status"] == "Accumulating":
        bull.append(f"Large wallets net buying: {usd(whales['buy_usd'])} bought vs {usd(whales['sell_usd'])} sold "
                    f"in big trades (≥{usd(whales['threshold'])}) last hour")
    if whales["status"] == "Distributing":
        bear.append(f"Large wallets net selling: {usd(whales['sell_usd'])} sold vs {usd(whales['buy_usd'])} bought "
                    f"in big trades (≥{usd(whales['threshold'])}) last hour")
    if top and top10 <= 20:
        bull.append(f"Spread-out ownership: top 10 holders (excluding pools) own {top10:.1f}%")
    if top and top10 >= 35:
        bear.append(f"Concentrated ownership: top 10 holders (excluding pools) own {top10:.1f}%")
    if m["ch_h1"] >= 50 and m["vol_h1"] < 0.3 * max(liq, 1):
        bear.append(f"Price up {pct(m['ch_h1'])} in 1h on thin volume ({usd(m['vol_h1'])} vs {usd(liq)} liquidity)")
    if unusual:
        (bull if br1 >= 0.5 else bear).append(
            f"Unusual volume: last hour {usd(m['vol_h1'])} vs {usd(m['vol_h24'] / 24)}/h 24h average")
    for w in whales["wash"][:2]:
        bear.append(f"Suspected wash trading: wallet {short(w)} both bought and sold 6+ times in the last hour")
    if rk:
        for f in rk["fails"]:
            bear.append(f"Rug check: {f}")
        if rk.get("dev_drop") is not None and rk["dev_drop"] >= 0.2:
            bear.append(f"Developer selling: creator balance down {rk['dev_drop']:.0%} since the bot started watching")
    if age_h is not None and age_h < 2:
        bear.append(f"Very new: {age_h * 60:.0f} minutes old")

    # risk level
    reasons, risk = [], "Moderate"
    if rk and not rk["ok"]:
        risk, reasons = "Critical", ["fails the rug check: " + "; ".join(rk["fails"])]
    elif liq_chg is not None and liq_chg <= -30:
        risk, reasons = "Critical", [f"liquidity fell {liq_chg:.0f}% in an hour"]
    elif rk and (rk.get("dev_drop") or 0) >= 0.2:
        risk, reasons = "Critical", ["developer is selling"]
    else:
        if liq < 20000:
            reasons.append(f"thin liquidity ({usd(liq)})")
        if top and top10 >= 35:
            reasons.append(f"top 10 holders own {top10:.0f}%")
        if rk and rk.get("insider_pct", 0) >= 5:
            reasons.append(f"insider-linked wallets hold {rk['insider_pct']:.1f}%")
        if age_h is not None and age_h < 2:
            reasons.append("under 2 hours old")
        if mc and liq / mc < 0.03:
            reasons.append(f"liquidity is only {liq / mc:.1%} of market cap")
        if whales["status"] == "Distributing" and br1 < 0.45:
            reasons.append("large wallets selling into weak demand")
        if reasons:
            risk = "High"
        elif (liq >= 250000 and ((rk or {}).get("holders") or 0) >= 5000 and top and top10 <= 20
              and age_h is not None and age_h >= 168 and len(bear) <= 1):
            risk, reasons = "Low", ["deep liquidity, 5,000+ holders, spread-out ownership, over a week old"]
        else:
            reasons = ["normal memecoin risk: no red flags, but no deep liquidity or long track record either"]

    if up_tfs >= 3 and (a6 is None or a6 >= 1.0) and br1 >= 0.5:
        momentum = "Strong"
    elif up_tfs <= 1 or (a6 is not None and a6 < 0.6) or br1 < 0.42:
        momentum = "Weakening" if m.get("ch_h24", 0) > 20 else "Weak"
    else:
        momentum = "Moderate"
    vol_trend = "Unknown" if a6 is None else "Increasing" if a6 >= 1.3 else "Declining" if a6 <= 0.6 else "Steady"
    if liq_chg is not None and liq_chg <= -15:
        liq_health = "Falling"
    elif liq < 20000 or (mc and liq / mc < 0.03):
        liq_health = "Thin"
    else:
        liq_health = "Healthy"
    net = len(bull) - len(bear)
    sentiment = "Bullish" if net >= 2 else "Bearish" if net <= -2 else "Neutral"

    growing = (g is not None and g >= 0.01) or whales["status"] == "Accumulating"
    if risk == "Critical":
        category = CATEGORIES[4]
    elif risk == "High":
        category = CATEGORIES[2]
    elif momentum == "Strong" and whales["status"] != "Distributing" and growing:
        category = CATEGORIES[0]
    elif momentum in ("Strong", "Moderate") and sentiment == "Bullish":
        category = CATEGORIES[1]
    else:
        category = CATEGORIES[3]

    have = [lv is not None, c.get("trades") is not None, bool(c.get("snaps") and len(c["snaps"]) >= 2),
            g is not None, rk is not None]
    confidence = "High" if sum(have) >= 4 else "Medium" if sum(have) >= 2 else "Low"

    score = (10 * up_tfs + min(a6 or 0, 3) * 8 + max(0, br1 - 0.45) * 80 + (10 if growing else 0)
             + (8 if liq_health == "Healthy" else 0) - 15 * RISK_ORDER[risk] - (10 if whales["status"] == "Distributing" else 0))
    return dict(mint=c["mint"], m=m, fm=fm, rk=rk, bull=bull, bear=bear, risk=risk, risk_reasons=reasons,
                momentum=momentum, vol_trend=vol_trend, liq_health=liq_health, liq_chg=liq_chg, sentiment=sentiment,
                category=category, confidence=confidence, have=have, whales=whales, levels=lv, br1=br1, br24=br24,
                a6=a6, a1=a1, g=g, top10=top10 if top else None, age_h=age_h, unusual=bool(unusual), score=score,
                sigma=volatility(c.get("px") or [], now), px_hist=c.get("px") or [])


# ---------------------------------------------------------------- scenarios
def dump_impact(holder_pct, mc, liq):
    """Illustrative price drop if holder_pct % of supply were sold into one constant-product pool."""
    if not (mc and liq and holder_pct):
        return None
    q_over_r = (holder_pct / 100 * mc) / (liq / 2)  # tokens sold vs tokens in the pool (both in USD at today's price)
    return 1 - 1 / (1 + q_over_r) ** 2


def scenarios(a):
    m, lv, w = a["m"], a["levels"], a["whales"]
    sig, n, span = a["sigma"]
    stat = (f"Statistical range (from {n} bot price samples over {span:.1f}h): a typical 1-hour move is "
            f"±{(math.exp(sig) - 1) * 100:.0f}%, a typical 6-hour move ±{(math.exp(sig * math.sqrt(6)) - 1) * 100:.0f}%."
            if sig else f"Not enough price history yet for a statistical range ({n} samples; needs 20+).")
    ref_hi = f"its 2h high of {usd(m['mcap'] * lv['hi'] / m['price'])} market cap" if lv else "recent highs"
    ref_lo = f"its 2h low of {usd(m['mcap'] * lv['lo'] / m['price'])} market cap" if lv else "recent lows"
    bull = ("<b>Bullish</b>: continues if buyers stay above ~55% of trades (now {:.0%}) and hourly volume holds at or above "
            "its 6h average{}. First reference: {}. Invalidated if it loses {} or buys fall under 45%.").format(
        a["br1"], f" (now {a['a6']:.1f}x)" if a["a6"] is not None else "", ref_hi, ref_lo)
    neutral = ("<b>Neutral</b>: if buying slows (5-min volume below the hourly pace, now {:.1f}x), expect it to range "
               "between {} and {}. Watch whether volume and holder count hold steady.").format(
        a["a1"], ref_lo, ref_hi)
    top1 = max((p for _, p in (a["rk"] or {}).get("top") or []), default=None)
    imp = dump_impact(top1, m["mcap"], m["liq"]) if top1 else None
    imp_txt = (f" If the largest non-pool holder ({top1:.1f}% of supply) sold at once, the price could drop roughly "
               f"{imp:.0%} (illustrative, constant-product pool math on {usd(m['liq'])} liquidity).") if imp else ""
    lp = (a["rk"] or {}).get("lp_locked")
    lp_txt = (f" Liquidity is {lp:.0f}% locked, which limits a sudden pull." if lp is not None and lp >= 90 else
              " Liquidity is not shown as locked: if it were pulled, the coin could become unsellable.")
    bear = ("<b>Bearish</b>: declines if sellers take over (buys under 45%), volume keeps fading, or large wallets keep "
            "selling (now: {}).{}{} Warning signs: liquidity falling, holder count dropping, price under {}.").format(
        w["status"].lower(), imp_txt, lp_txt, ref_lo)
    return [bull, neutral, bear, stat + " These are scenarios, not predictions."]


# ---------------------------------------------------------------- rendering
RISK_ICON = {"Low": "🟢", "Moderate": "🟡", "High": "🟠", "Critical": "🔴"}
CAT_ICON = {CATEGORIES[0]: "🟢", CATEGORIES[1]: "🟢", CATEGORIES[2]: "🟡", CATEGORIES[3]: "⚪", CATEGORIES[4]: "🔴"}
VERDICT = {
    CATEGORIES[0]: "The evidence supports further research: momentum, flow and holder/whale signals line up.",
    CATEGORIES[1]: "Momentum is there but not fully confirmed: wait for volume and holders to keep up.",
    CATEGORIES[2]: "Speculative: risk flags outweigh the setup. Only with money you can afford to lose.",
    CATEGORIES[3]: "No clear edge right now: momentum or participation is lacking.",
    CATEGORIES[4]: "Avoid: critical warning signs.",
}


def rank_line(i, a):
    m = a["m"]
    return (f"{i}. {CAT_ICON[a['category']]} <b>${esc(m['symbol'])}</b> · {a['category']}\n"
            f"    Momentum {a['momentum']} · Volume {a['vol_trend']} · Whales {a['whales']['status']} · "
            f"Risk {RISK_ICON[a['risk']]} {a['risk']} · MC {usd(m['mcap'])}")


def detail(i, a, now, stamp):
    m, w, rk = a["m"], a["whales"], a["rk"] or {}
    fdv = f" · FDV {usd(m['fdv'])}" if m.get("fdv") and abs(m["fdv"] - m["mcap"]) > 1 else ""
    age = f"{a['age_h']:.1f}h" if a["age_h"] is not None and a["age_h"] < 48 else \
        f"{a['age_h'] / 24:.0f} days" if a["age_h"] is not None else "unknown"
    holders = rk.get("holders") or (a["fm"] or {}).get("holders") or "unknown"
    L = [f"🔎 <b>#{i} ${esc(m['symbol'])}</b> · {a['category']} · data {stamp}",
         f"<code>{esc(a['mint'])}</code>",
         "",
         "<b>Market</b>",
         f"Price {m['price']:.10g} · MC {usd(m['mcap'])}{fdv} · Liquidity {usd(m['liq'])}",
         f"24h volume {usd(m.get('vol_h24', 0))} · Age {age} · Holders {holders}",
         f"5m {pct(m['ch_m5'])} · 1h {pct(m['ch_h1'])} · 6h {pct(m['ch_h6'])} · 24h {pct(m.get('ch_h24', 0))}",
         "",
         "🟢 <b>Bullish</b>"]
    L += [f"• {esc(b)}" for b in a["bull"]] or ["• None found"]
    L += ["", "🔴 <b>Bearish</b>"] + ([f"• {esc(b)}" for b in a["bear"]] or ["• None found"])
    L += ["", f"{RISK_ICON[a['risk']]} <b>Risk: {a['risk']}</b>: " + esc("; ".join(a["risk_reasons"]))]
    L += ["", "🐋 <b>Large wallets</b> " + (f"(trades ≥{usd(w['threshold'])}, from GeckoTerminal)" if w["has_trades"]
                                          else "(trade data unavailable this run)")]
    pl = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    L.append(f"Last hour: {pl(w['n_buyers'], 'big buyer')} {usd(w['buy_usd'])} · {pl(w['n_sellers'], 'big seller')} "
             f"{usd(w['sell_usd'])} → <b>{w['status']}</b>")
    for t in w["wallets"][:4]:
        held = f" · holds {t['pct']:.1f}% of supply" if t.get("pct") else ""
        L.append(f"• {short(t['wallet'])} {t['kind'].upper()} {usd(t['usd'])} · {ago(now - t['ts'])}{held}")
    for owner, d, nowp, span in w["holder_moves"][:3]:
        L.append(f"• Top holder {short(owner)} {'added' if d > 0 else 'cut'} {abs(d):.2f}% of supply over "
                 f"{span / 60:.0f} min (now {nowp:.2f}%)")
    if w["new_wallets"]:
        L.append(f"New wallets: {w['new_wallets'][0]} of {w['new_wallets'][1]} wallets trading ≥$50 in the last hour "
                 "hadn't traded it in the prior 23h")
    L.append("Confirmed = on-chain trades above. Wallet identities are not known; no influencer links are claimed.")
    L += [""] + scenarios(a)
    upside = "unclear" if a["momentum"] in ("Weak", "Weakening") else "present" if a["sentiment"] == "Bullish" else "limited"
    L += ["", "<b>Assessment</b>",
          f"Sentiment {a['sentiment']} · Risk {a['risk']} · Momentum {a['momentum']} · Whales {w['status']}",
          f"Liquidity {a['liq_health']} · Upside {upside} · Downside: see bearish case · Confidence {a['confidence']}",
          f"{CAT_ICON[a['category']]} <b>{a['category']}</b>: {VERDICT[a['category']]}",
          "Not a buy instruction."]
    return "\n".join(L)


def market_wide(rows, now, stamp, analyzed):
    bull = [a for a in rows if a["sentiment"] == "Bullish"]
    bear = [a for a in rows if a["sentiment"] == "Bearish"]
    unusual = [a for a in rows if a["unusual"]]
    acc = [a for a in rows if a["whales"]["status"] == "Accumulating"]
    dist = [a for a in rows if a["whales"]["status"] == "Distributing"]
    crit = [a for a in rows if a["risk"] in ("Critical", "High")]
    falling_liq = [a for a in rows if a["liq_chg"] is not None and a["liq_chg"] <= -15]
    breadth = (len(bull) - len(bear)) / max(len(rows), 1)
    mood = "risk-on" if breadth >= 0.25 else "risk-off" if breadth <= -0.25 else "mixed"
    names = lambda xs: ", ".join("$" + esc(a["m"]["symbol"]) for a in xs[:5]) or "none"
    L = [f"🌐 <b>Market-wide</b> · data {stamp}",
         f"Coins analyzed: {analyzed} (shown: {len(rows)} with enough data)",
         f"🟢 Bullish signals: {len(bull)} · 🔴 Bearish: {len(bear)} · Mood: <b>{mood}</b>",
         f"Unusual volume: {names(unusual)}",
         f"Whales accumulating: {names(acc)}",
         f"Whales distributing: {names(dist)}",
         f"Liquidity being withdrawn: {names(falling_liq)}",
         f"High/critical risk among trending: {len(crit)} of {len(rows)}"]
    risks = []
    if crit:
        risks.append(f"{len(crit)} trending coins carry high or critical risk")
    if falling_liq:
        risks.append("liquidity pulls on " + names(falling_liq))
    if dist:
        risks.append("large wallets selling " + names(dist))
    L.append("Biggest risks: " + ("; ".join(risks) if risks else "nothing unusual right now"))
    picks = []
    good = [a for a in rows if a["category"] in CATEGORIES[:2]]
    if good:
        picks.append((good[0], "best-supported setup: " + (good[0]["bull"][0] if good[0]["bull"] else "momentum")))
    hot_bad = sorted((a for a in rows if a["risk"] in ("Critical", "High")), key=lambda a: -a["m"].get("vol_h24", 0))
    if hot_bad:
        a = hot_bad[0]
        picks.append((a, f"heavily traded but {a['risk'].lower()} risk: {a['risk_reasons'][0]}"))
    fading = [a for a in rows if a["momentum"] == "Weakening"]
    if fading:
        a = fading[0]
        picks.append((a, "losing traction: " + (a["bear"][0] if a["bear"] else "momentum weakening")))
    L.append("")
    L.append("<b>Most noteworthy</b>")
    for a, why in picks[:3]:
        L.append(f"• <b>${esc(a['m']['symbol'])}</b>: {esc(why)}")
    if not picks:
        L.append("• Nothing stands out right now.")
    L.append("")
    L.append("Not available: " + "; ".join(NOT_AVAILABLE) + ".")
    return "\n".join(L)


def build_report(coins, now, stamp, max_rows=8):
    """coins: list of input dicts. Returns (messages for /summary, {n: detail message})."""
    rows = [analyze(c, now) for c in coins]
    rows = [a for a in rows if a["rk"] is not None and a["m"]["liq"] >= 5000 and a["m"]["b1"] + a["m"]["s1"] >= 20]
    rows.sort(key=lambda a: -a["score"])
    shown = rows[:max_rows]
    head = [f"📊 <b>TRENDING MARKET REPORT</b> · data {stamp}",
            "Ranked by momentum, flow, liquidity, holder and whale activity, minus risk. Popular ≠ best."]
    head += [rank_line(i + 1, a) for i, a in enumerate(shown)] or ["Nothing trending has enough data right now."]
    if shown:
        head.append(f"\nFull breakdown: /summary 1 … /summary {len(shown)}")
    msgs = ["\n".join(head), market_wide(shown, now, stamp, len(coins))]
    details = {i + 1: detail(i + 1, a, now, stamp) for i, a in enumerate(shown)}
    if details:
        msgs.append(details[1])
    return msgs, details, shown
