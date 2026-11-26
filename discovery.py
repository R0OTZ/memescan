"""/best: find the strongest CURRENT opportunities.

1. Pull candidates from every live source (fomo trending + gainers, DexScreener boosts/new profiles,
   GeckoTerminal trending, fresh PumpSwap graduations, top PumpSwap volume).
2. One batched DexScreener call prices them all; anything without trades in the last 5 minutes, thin, or
   dead is dropped (that is what kept old coins on the old list).
3. Unusual volume and holder growth are tagged from the live numbers and the bot's own history.
4. The best ~10 by market data get the full analysis (RugCheck, on-chain mint, Jupiter sell route, and
   GeckoTerminal trades for the top few), then are ranked by the shared token_intel score.
Only tokens that are not disqualified and score at least MIN_SCORE are shown; if none qualify, /best says so.
"""
import html
import time

import providers as P
import token_intel as T

CFG = dict(min_liq=10_000, min_mcap=30_000, max_mcap=50_000_000, min_tx_m5=2, min_tx_h1=30,
           deep=10, deep_trades=3, show=5, min_score=55)


def gather(cache=None):
    """Candidate mints with the reasons they were picked up. Returns (order, tags, fomo, src)."""
    tags, order, src = {}, [], {}

    def add(mint, tag):
        if not mint or not isinstance(mint, str) or mint == P.WSOL:
            return
        if mint not in tags:
            tags[mint] = []
            order.append(mint)
        if tag not in tags[mint]:
            tags[mint].append(tag)

    fr = P.fomo_feed(cache)
    src["fomo"] = T.meta(fr)
    fomo = fr.get("data") or {}
    for m in sorted(fomo, key=lambda k: -fomo[k]["vol1"])[:25]:
        if fomo[m]["vol1"] > 0:
            add(m, "fomo trending")
    for m in sorted((k for k in fomo if fomo[k]["vol1"] >= 5000), key=lambda k: -fomo[k]["ch1"])[:15]:
        if fomo[m]["ch1"] > 0:
            add(m, "fomo gainer")
    for path, tag in (("/token-boosts/top/v1", "boosted"), ("/token-boosts/latest/v1", "boosted"),
                      ("/token-profiles/latest/v1", "new profile")):
        r = P.ds_list(path, cache)
        src["DexScreener " + tag] = T.meta(r)
        for it in (r.get("data") or [])[:40] if isinstance(r.get("data"), list) else []:
            if it.get("chainId") == "solana":
                add(it.get("tokenAddress"), tag)
    for path, tag in (("/networks/solana/trending_pools?page=1", "trending"),
                      ("/networks/solana/new_pools?include=dex&page=1", "just graduated"),
                      ("/networks/solana/dexes/pumpswap/pools?sort=h24_volume_usd_desc&page=1", "graduated")):
        r = P.gt(path, cache, 180)
        src["GeckoTerminal " + tag] = T.meta(r)
        for p in ((r.get("data") or {}).get("data") or []) if r["ok"] else []:
            rel = p.get("relationships") or {}
            if "new_pools" in path and ((rel.get("dex") or {}).get("data") or {}).get("id") != "pumpswap":
                continue
            bid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            if bid.startswith("solana_"):
                add(bid[len("solana_"):], tag)
    return order, tags, fomo, src


def prefilter(order, tags, ds, hist, now, fomo=None):
    fomo = fomo or {}
    """Market-data gate and a cheap pre-score. Returns ([(pre, mint)], rejected_counts)."""
    keep, rej = [], {}

    def no(why):
        rej[why] = rej.get(why, 0) + 1
    for mint in order:
        pairs = (ds.get("data") or {}).get(mint)
        p = P.best_pair(pairs)
        if not p:
            no("no trading pair")
            continue
        m = T.pair_metrics(p, now)
        if m["liq"] < CFG["min_liq"]:
            no(f"liquidity under {T.usd(CFG['min_liq'])}")
            continue
        if not CFG["min_mcap"] <= m["mcap"] <= CFG["max_mcap"]:
            no("market cap outside range")
            continue
        if m["tx_m5"] < CFG["min_tx_m5"]:
            no("no trades in the last 5 min (stale)")
            continue
        if m["tx_h1"] < CFG["min_tx_h1"]:
            no("fewer than 30 trades in 1h")
            continue
        accel = m["vol_m5"] * 12 / m["vol_h1"] if m["vol_h1"] else 0
        if accel >= 2 and m["vol_m5"] >= 1000:
            tags[mint].append("volume spike")
        h = hist.get(mint) or []
        old = T.sample_near(h, now, 3600, 1500) if h else None
        cur = (fomo.get(mint) or {}).get("holders")
        if old and old.get("holders") and cur and cur > old["holders"] * 1.03:
            tags[mint].append(f"holders +{(cur / old['holders'] - 1) * 100:.0f}%/h")
        br = m["b1"] / m["tx_h1"]
        pre = (50 + (br - 0.5) * 120 + min(accel, 3) * 8 + T.clamp(m["ch_m5"], -8, 8)
               + (8 if 0 < m["ch_h1"] <= 80 else -10 if m["ch_h1"] > 150 or m["ch_h1"] < -20 else 0)
               + T.clamp((m["liq"] - 10000) / 10000, 0, 10) + min(m["tx_h1"] / 50, 10))
        keep.append((pre, mint))
    keep.sort(key=lambda x: -x[0])
    return keep, rej


def run(cache=None, history=None, weights=None, gather_result=None):
    """Full /best pass. history: token_intel.History. Returns dict(ranked, shown, rejected, n, src, t, tags)."""
    t0 = time.time()
    order, tags, fomo, src = gather_result or gather(cache)
    ds = P.ds_tokens(order, cache)
    hist = history or T.History()
    pre, rej = prefilter(order, tags, ds, {m: hist.get(m) for m in order}, time.time(), fomo)
    ranked = []
    for i, (_, mint) in enumerate(pre[:CFG["deep"]]):
        b = T.collect(mint, cache, deep=True, ds=ds, fomo=fomo, trades=i < CFG["deep_trades"])
        a = T.analyze(b, hist.get(mint), weights)
        a["tags"] = tags.get(mint, [])
        hist.add(mint, a)
        ranked.append(a)
    for _, mint in pre[CFG["deep"]:]:
        rej["ranked below the top 10 on market data"] = rej.get("ranked below the top 10 on market data", 0) + 1
    ranked.sort(key=lambda a: (not a["disqualified"], a["score"]), reverse=True)
    shown = [a for a in ranked if not a["disqualified"] and a["score"] >= CFG["min_score"]
             and a["verdict"] != T.VERDICTS[2]][:CFG["show"]]
    for a in ranked:
        if a["disqualified"]:
            rej["failed security (disqualified)"] = rej.get("failed security (disqualified)", 0) + 1
        elif a not in shown:
            rej[f"score under {CFG['min_score']} or too much data missing"] = \
                rej.get(f"score under {CFG['min_score']} or too much data missing", 0) + 1
    return dict(ranked=ranked, shown=shown, rejected=rej, n=len(order), src=src, t=time.time(),
                took=time.time() - t0, tags=tags, market_src=T.meta(ds))


REJECT_SHORT = {"no trading pair": "no market", "market cap outside range": "too big/small",
                "no trades in the last 5 min (stale)": "no recent trades", "fewer than 30 trades in 1h": "too quiet",
                "ranked below the top 10 on market data": "weaker numbers", "failed security (disqualified)": "failed safety"}


def fmt(res):
    """Telegram messages for /best: a short header, then one short card per coin."""
    shown = res["shown"]
    rej = res["rejected"]
    skipped = ", ".join(f"{v} {REJECT_SHORT.get(k, 'low liquidity' if 'liquidity' in k else 'weak score' if 'score' in k else 'dropped on re-check' if 're-check' in k else k)}"
                        for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:4])
    head = (f"🏆 <b>Best right now</b> · {T.hm(res['t'])}\n"
            f"Checked {res['n']} coins → {len(shown)} pass" + (f"\nSkipped: {skipped}" if skipped else ""))
    down = [k for k, v in res["src"].items() if not v["ok"]]
    if down:
        head += "\n⚠️ Some sources were down: " + html.escape(", ".join(down))
    head += "\n<i>Not included: X/Twitter buzz, Bubblemaps, Thesis (no free data). Scores aren't promises.</i>"
    if not shown:
        near = [a for a in res["ranked"] if not a["disqualified"]][:3]
        tail = ("\nClosest: " + ", ".join(f"${html.escape(a['market']['symbol'])} {a['score']}" for a in near)) if near else ""
        return [head + "\n\n<b>Nothing is good enough right now.</b> I won't pad the list with weak coins." + tail]
    msgs = [head]
    for i, a in enumerate(shown, 1):
        msgs.append(T.fmt_best_item(i, a) + ("\n📍 Found via " + html.escape(", ".join(a.get("tags") or [])) if a.get("tags") else ""))
    return msgs


def refresh(mints, cache=None, history=None, weights=None, tags=None):
    """Re-analyze a short list with fresh market data (used when /best is asked, so prices are seconds old)."""
    ds = P.ds_tokens(mints, cache)
    fomo = (P.fomo_feed(cache).get("data") or {})
    out = []
    hist = history or T.History()
    for mint in mints:
        b = T.collect(mint, cache, deep=True, ds=ds, fomo=fomo, trades=False)
        a = T.analyze(b, hist.get(mint), weights)
        a["tags"] = (tags or {}).get(mint, [])
        out.append(a)
    return out
