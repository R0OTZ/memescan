#!/usr/bin/env python3
"""memescan: Solana memecoin scanner for fomo traders.

Every run:
  1. Pulls trending Solana tokens (DexScreener boosts/profiles + GeckoTerminal trending).
  2. Filters on market data, then rug-checks survivors (RugCheck: mint/freeze authority,
     danger risks, insider wallet clusters, LP, creator history).
  3. Gives each a verdict: BUY / WAIT (with dip target) / AVOID (with reasons).
  4. Watches YOUR wallet's holdings and sends SELL / STOP / TAKE-PROFIT alerts,
     including "dev is selling" (creator balance drop).
  5. Logs every call and tracks how it played out, so /stats tells you if it actually works.

You execute trades yourself on fomo. Standard library only. No paid APIs.
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

STATE_PATH = os.environ.get("STATE_PATH", "state/state_v2.json")
SOL_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RC = "https://api.rugcheck.xyz/v1"
UA = {"User-Agent": "Mozilla/5.0 (memescan)", "Accept": "application/json"}

CFG = dict(
    stake_usd=float(os.environ.get("STAKE_USD", "2")),
    min_liq=15_000,          # USD liquidity floor: below this, slippage eats you and rugs are cheap
    min_mcap=50_000,
    max_mcap=20_000_000,
    min_age_min=15,          # younger than this = too new to judge
    min_h1_txns=40,          # ignore dead tokens
    max_insider_pct=10.0,    # % supply held by wallets RugCheck links to insiders
    overextended_h1=120.0,   # +120% in 1h = chasing; wait for a dip
    dip_factor=0.75,         # dip target = 75% of current price
    buy_score=65,
    stop_pct=-18.0,
    tp_mult=2.0,
    trail_from_peak=-25.0,   # after take-profit, sell if price falls 25% from peak
    dev_sell_drop=0.20,      # creator balance down 20%+ = dev selling
    max_rugchecks=30,        # per run, to stay polite to the free API
    realert_hours=3,
    watch_hours=6,
)
TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWXJN3YUiR1GaEz",  # USDT (as listed by Tether)
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWX2ZCG7FrPKdDhUmJ",
           "So11111111111111111111111111111111111111112"}  # wrapped SOL

NOW = time.time()
ACCTS = {}  # mint -> your token account addresses (filled by wallet_tokens)
USD_STABLES = ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWXJN3YUiR1GaEz",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY2QdWX2ZCG7FrPKdDhUmJ")
WSOL = "So11111111111111111111111111111111111111112"
ATA_RENT_SOL = 0.00203928  # refundable rent when a token account is created; not part of your cost
BAL = {}  # stablecoin balance found in your wallet this run
_SOLP = []


def log(msg):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or "\0"
    wallet = os.environ.get("WALLET") or "\0"
    print(str(msg).replace(tok, "***").replace(wallet, "<wallet>"), flush=True)


def get_json(url, method="GET", body=None, timeout=15, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    hdr = dict(UA)
    if data:
        hdr["Content-Type"] = "application/json"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            log(f"HTTP {e.code} {url.split('?')[0][:80]}")
            return None
        except Exception as e:  # network, timeout, bad JSON
            if i < tries - 1:
                time.sleep(1.5 * (i + 1))
                continue
            log(f"ERR {url.split('?')[0][:80]}: {type(e).__name__}")
            return None


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {}
    for k, v in dict(positions={}, watch={}, calls=[], alerted={}, tg_offset=0,
                     chat_id=None, last_judged=[]).items():
        s.setdefault(k, v)
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    s["calls"] = s["calls"][-2000:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- data
def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("data", "pairs", "items"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def discover():
    seen, order = {}, []

    def add(mint, tag):
        if not mint or not isinstance(mint, str) or mint in STABLES:
            return
        if mint not in seen:
            seen[mint] = []
            order.append(mint)
        if tag not in seen[mint]:
            seen[mint].append(tag)

    for path, tag in (("/token-boosts/top/v1", "boosted"),
                      ("/token-boosts/latest/v1", "boosted"),
                      ("/token-profiles/latest/v1", "new profile")):
        for it in as_list(get_json(DS + path))[:40]:
            if it.get("chainId") == "solana":
                add(it.get("tokenAddress"), tag)
    for p in as_list(get_json(GT + "/networks/solana/trending_pools?page=1")):
        bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if bid.startswith("solana_"):
            add(bid[len("solana_"):], "gecko trending")
    # graduated = pump.fun coins that filled their bonding curve and moved to PumpSwap
    for url, tag in ((GT + "/networks/solana/new_pools?include=dex&page=1", "just graduated"),
                     (GT + "/networks/solana/new_pools?include=dex&page=2", "just graduated"),
                     (GT + "/networks/solana/dexes/pumpswap/pools?sort=h24_volume_usd_desc&page=1", "graduated")):
        for p in as_list(get_json(url)):
            rel = p.get("relationships") or {}
            dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
            if "new_pools" in url and dex != "pumpswap":
                continue
            bid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            if bid.startswith("solana_"):
                add(bid[len("solana_"):], tag)
    return order, seen


def pairs_for(mints):
    best = {}
    mints = list(dict.fromkeys(m for m in mints if m))
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        for p in as_list(get_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}")):
            m = (p.get("baseToken") or {}).get("address")
            if m not in chunk:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            cur = float((best.get(m, {}).get("liquidity") or {}).get("usd") or 0) if m in best else -1
            if liq > cur:
                best[m] = p
    return best


def metrics(p):
    def f(d, k):
        try:
            return float((p.get(d) or {}).get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    tx = p.get("txns") or {}

    def bs(w):
        t = tx.get(w) or {}
        return int(t.get("buys") or 0), int(t.get("sells") or 0)

    b1, s1 = bs("h1")
    b5, s5 = bs("m5")
    created = p.get("pairCreatedAt")
    return dict(
        symbol=(p.get("baseToken") or {}).get("symbol") or "?",
        price=float(p.get("priceUsd") or 0),
        liq=f("liquidity", "usd"),
        mcap=float(p.get("marketCap") or p.get("fdv") or 0),
        vol_h1=f("volume", "h1"),
        ch_m5=f("priceChange", "m5"), ch_h1=f("priceChange", "h1"), ch_h6=f("priceChange", "h6"),
        b1=b1, s1=s1, b5=b5, s5=s5,
        age_min=(NOW * 1000 - created) / 60000 if created else None,
        url=p.get("url") or "",
    )


def rugcheck(mint):
    r = get_json(f"{RC}/tokens/{mint}/report")
    if not isinstance(r, dict):
        return None
    out = dict(fails=[], warns=[], insider_pct=0.0, insider_wallets=0,
               score_norm=float(r.get("score_normalised") or 0),
               creator=r.get("creator"), creator_bal=r.get("creatorBalance"))
    if r.get("rugged"):
        out["fails"].append("already marked rugged")
    if r.get("mintAuthority"):
        out["fails"].append("dev can still mint new tokens")
    if r.get("freezeAuthority"):
        out["fails"].append("dev can freeze your tokens")
    for k in r.get("risks") or []:
        lvl, name = (k.get("level") or "").lower(), k.get("name") or "unknown risk"
        if lvl == "danger":
            out["fails"].append(name)
        elif lvl == "warn":
            out["warns"].append(name)
    holders = r.get("topHolders") or []
    pcts = [float(h.get("pct") or 0) for h in holders]
    scale = 100.0 if pcts and max(pcts) <= 1.0 else 1.0  # tolerate fraction or percent
    out["insider_pct"] = sum(float(h.get("pct") or 0) * scale for h in holders if h.get("insider"))
    out["insider_wallets"] = int(r.get("graphInsidersDetected") or 0)
    if out["insider_pct"] >= CFG["max_insider_pct"]:
        out["fails"].append(f"linked insider wallets hold {out['insider_pct']:.1f}% of supply")
    out["ok"] = not out["fails"]
    return out


def wallet_tokens(owner):
    """Returns {mint: amount} or None if the RPC failed (never confuse failure with 'sold everything')."""
    res, failed = {}, 0
    for prog in TOKEN_PROGRAMS:
        r = get_json(SOL_RPC, "POST", {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                       "params": [owner, {"programId": prog}, {"encoding": "jsonParsed"}]})
        val = ((r or {}).get("result") or {}).get("value") if isinstance(r, dict) else None
        if val is None:
            failed += 1
            continue
        for a in val:
            try:
                info = a["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"].get("uiAmount") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if info["mint"] in USD_STABLES:
                BAL["usd"] = BAL.get("usd", 0) + amt
            if amt > 0 and info["mint"] not in STABLES:
                res[info["mint"]] = res.get(info["mint"], 0) + amt
                ACCTS.setdefault(info["mint"], []).append(a.get("pubkey"))
    return None if failed == len(TOKEN_PROGRAMS) else res


def rpc(method, params):
    r = get_json(SOL_RPC, "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    time.sleep(0.15)  # public RPC is rate-limited
    return (r or {}).get("result") if isinstance(r, dict) else None


def sol_usd():
    if not _SOLP:
        p = pairs_for([WSOL]).get(WSOL)
        _SOLP.append(float(p.get("priceUsd") or 0) if p else 0.0)
    return _SOLP[0]


def buying_power(owner):
    """USD you can spend: stablecoins + SOL (keeping ~0.01 SOL for fees). None if unknown."""
    if not owner:
        return None
    r = rpc("getBalance", [owner])
    if not isinstance(r, dict):
        return None
    sol = (r.get("value") or 0) / 1e9
    sp = sol_usd()
    stable = BAL.get("usd", 0.0)
    return dict(usd=stable + max(0.0, sol - 0.01) * sp, stable=stable, sol=sol, sol_usd=sol * sp)


def entry_from_chain(owner, mint, sol_price):
    """Average USD price you actually paid, read from your buy transactions. None if it can't tell."""
    sigs = []
    for acct in ACCTS.get(mint, []):
        for s in rpc("getSignaturesForAddress", [acct, {"limit": 25}]) or []:
            if not s.get("err"):
                sigs.append(s["signature"])
    tokens_in = cost = 0.0
    for sig in sigs[:25]:
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre, post = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

        def bal(lst, m):
            return sum(float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                       for b in lst if b.get("owner") == owner and b.get("mint") == m)

        got = bal(post, mint) - bal(pre, mint)
        if got <= 0:
            continue  # a sell or transfer out
        spent = sum(max(0.0, bal(pre, q) - bal(post, q)) for q in USD_STABLES)
        spent += max(0.0, bal(pre, WSOL) - bal(post, WSOL)) * sol_price
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
        if owner in keys:
            i = keys.index(owner)
            try:
                dsol = (meta["preBalances"][i] - meta["postBalances"][i]) / 1e9
            except (KeyError, IndexError, TypeError):
                dsol = 0.0
            if not any(b.get("owner") == owner and b.get("mint") == mint for b in pre):
                dsol -= ATA_RENT_SOL  # account was opened in this tx
            spent += max(0.0, dsol) * sol_price
        if spent <= 0:
            continue  # airdrop or transfer in: cost unknown
        tokens_in += got
        cost += spent
    return cost / tokens_in if tokens_in > 0 else None


# ---------------------------------------------------------------- judging
def market_gate(m):
    if m["price"] <= 0:
        return "skip", ["no price"]
    if m["b1"] + m["s1"] < CFG["min_h1_txns"]:
        return "skip", ["dead: few trades"]
    if m["liq"] < CFG["min_liq"]:
        return "AVOID", [f"liquidity only ${m['liq']:,.0f}: easy to rug, high slippage"]
    if m["mcap"] and not (CFG["min_mcap"] <= m["mcap"] <= CFG["max_mcap"]):
        return "skip", ["outside market-cap band"]
    if m["age_min"] is not None and m["age_min"] < CFG["min_age_min"]:
        return "WAIT", [f"only {m['age_min']:.0f} min old: too new to judge"]
    return "ok", []


def momentum_score(m, rk):
    tot = m["b1"] + m["s1"]
    br = m["b1"] / tot if tot else 0.5
    s = 50 + (br - 0.5) * 100
    turn = m["vol_h1"] / m["liq"] if m["liq"] else 0
    s += min(turn, 3) * 7
    s += max(min(m["ch_m5"], 10), -10)
    if 0 < m["ch_h1"] <= 60:
        s += 10
    elif m["ch_h1"] < 0:
        s += max(m["ch_h1"], -30) * 0.5
    s -= 4 * len(rk["warns"])
    s -= max(0.0, rk["score_norm"] - 30) * 0.3
    if m["age_min"] is not None and m["age_min"] < 60:
        s -= 5
    return max(0, min(100, round(s))), br, turn


def judge(m, rk):
    if not rk["ok"]:
        return dict(verdict="AVOID", score=0, reasons=rk["fails"])
    score, br, turn = momentum_score(m, rk)
    if br < 0.40:
        return dict(verdict="AVOID", score=score, reasons=[f"sellers in control ({br:.0%} buys last hour)"])
    if m["ch_h1"] > CFG["overextended_h1"]:
        return dict(verdict="WAIT", score=score, target=m["price"] * CFG["dip_factor"],
                    reasons=[f"up {m['ch_h1']:.0f}% in 1h: buying now is chasing"])
    if m["ch_m5"] < -8:
        return dict(verdict="WAIT", score=score, target=m["price"] * 0.9,
                    reasons=[f"dropping right now ({m['ch_m5']:.0f}% in 5m)"])
    if score >= CFG["buy_score"]:
        return dict(verdict="BUY", score=score, reasons=[f"{br:.0%} buys, volume {turn:.1f}x liquidity in 1h"])
    return dict(verdict="WAIT", score=score, reasons=["no clear momentum yet"])


# ---------------------------------------------------------------- formatting
def usd(x):
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:,.2f}"


def px(x):
    if x == 0:
        return "$0"
    if x >= 1:
        return f"${x:,.4f}"
    s = f"{x:.12f}".rstrip("0")
    head = len(s) - len(s.lstrip("0."))
    return "$" + f"{x:.{max(4, head + 2)}f}"


def odds_line(state, score):
    """Prediction from the bot's own record: how often similar BUY calls hit 2x before the stop."""
    past = [c for c in state["calls"] if c["verdict"] == "BUY" and c.get("hit") in ("2x", "stop")
            and abs(c["score"] - score) <= 10]
    be = -CFG["stop_pct"] / ((CFG["tp_mult"] - 1) * 100 - CFG["stop_pct"])
    if len(past) < 15:
        return f"Odds: not enough history yet ({len(past)}/15 similar calls resolved)"
    w = sum(c["hit"] == "2x" for c in past)
    return (f"Odds: {w}/{len(past)} similar past calls ({w / len(past):.0%}) hit 2x before the stop "
            f"(break-even \u2248{be:.0%} before fees)")


def card(mint, m, rk, j, tags):
    e = html.escape
    icon = {"BUY": "🟢", "WAIT": "🟡", "AVOID": "🔴"}[j["verdict"]]
    age = f"{m['age_min'] / 60:.1f}h" if m["age_min"] is not None else "?"
    lines = [f"{icon} <b>{j['verdict']} ${e(m['symbol'])}</b>  score {j['score']}",
             f"Price {px(m['price'])} · MC {usd(m['mcap'])} · Liq {usd(m['liq'])} · age {age}",
             f"5m {m['ch_m5']:+.0f}% · 1h {m['ch_h1']:+.0f}% · 1h txns {m['b1']}B/{m['s1']}S",
             "Why: " + e("; ".join(j["reasons"]))]
    if rk:
        rc = "passed" if rk["ok"] else "FAILED"
        extra = f", warnings: {e(', '.join(rk['warns'][:3]))}" if rk["warns"] else ""
        lines.append(f"Rug check: {rc} (insiders {rk['insider_pct']:.1f}%{extra})")
    if j["verdict"] == "BUY":
        stop = m["price"] * (1 + CFG["stop_pct"] / 100)
        lines.append(f"Plan: ${CFG['stake_usd']:.0f} in. At {CFG['tp_mult']:.0f}x sell at least your stake back. "
                     f"Stop {CFG['stop_pct']:.0f}% ({px(stop)}).")
    if j.get("odds"):
        lines.append(j["odds"])
    if j.get("power"):
        lines.append(j["power"])
    if any("graduated" in t for t in tags):
        lines.append("Graduated: finished its pump.fun curve and now trades on PumpSwap.")
    if j.get("target"):
        lines.append(f"Entry: wait for {px(j['target'])}. I'll ping you if it gets there and still looks safe.")
    if "boosted" in tags:
        lines.append("Note: trending partly because someone paid DexScreener to boost it.")
    lines.append(f"<code>{e(mint)}</code>")
    if m["url"]:
        lines.append(e(m["url"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram
def tg(method, **params):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return None
    return get_json(f"https://api.telegram.org/bot{tok}/{method}", "POST", params)


def send(state, text):
    chat = os.environ.get("TELEGRAM_CHAT_ID") or state.get("chat_id")
    if not chat:
        log("no chat yet: message your bot /start")
        return
    tg("sendMessage", chat_id=chat, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True)


def read_commands(state):
    r = tg("getUpdates", offset=state["tg_offset"] + 1, timeout=0)
    cmds = []
    for u in (r or {}).get("result") or []:
        state["tg_offset"] = max(state["tg_offset"], u.get("update_id", 0))
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip().split("@")[0].lower()
        allowed = os.environ.get("TELEGRAM_CHAT_ID") or state.get("chat_id")
        if not allowed and chat:
            state["chat_id"] = str(chat)  # first person to message the bot owns it
            send(state, "🔒 Locked to this chat. Commands: /scan /positions /stats")
            allowed = state["chat_id"]
        if chat and str(chat) == str(allowed) and text.startswith("/"):
            cmds.append(text.split()[0])
    return cmds


# ---------------------------------------------------------------- tracking
def record_call(state, mint, m, j):
    state["calls"].append(dict(ts=NOW, mint=mint, sym=m["symbol"], verdict=j["verdict"], price=m["price"],
                               score=j["score"], max=m["price"], min=m["price"], last=m["price"], hit=None))


def update_calls(state, prices):
    for c in state["calls"]:
        if NOW - c["ts"] > 86400 or c["mint"] not in prices:
            continue
        p = prices[c["mint"]]
        c["max"], c["min"], c["last"] = max(c["max"], p), min(c["min"], p), p
        if c["hit"] is None and c["price"]:
            if p >= c["price"] * CFG["tp_mult"]:
                c["hit"] = "2x"
            elif p <= c["price"] * (1 + CFG["stop_pct"] / 100):
                c["hit"] = "stop"


def stats_text(state):
    calls = state["calls"]
    if not calls:
        return "No calls logged yet."
    out = [f"<b>Call log</b> ({len(calls)} calls since {time.strftime('%b %d', time.gmtime(calls[0]['ts']))})"]
    for v in ("BUY", "WAIT", "AVOID"):
        cs = [c for c in calls if c["verdict"] == v]
        if not cs:
            continue
        win = sum(c["hit"] == "2x" for c in cs)
        stop = sum(c["hit"] == "stop" for c in cs)
        crash = sum(c["min"] <= c["price"] * 0.5 for c in cs)
        out.append(f"{v}: {len(cs)} · hit 2x first {win} · hit stop first {stop} · fell 50%+ {crash}")
    buys = [c for c in calls if c["verdict"] == "BUY" and (c["hit"] or NOW - c["ts"] > 86400)]
    if buys:
        stake = CFG["stake_usd"]
        pnl = 0.0
        for c in buys:
            if c["hit"] == "2x":
                pnl += stake * (CFG["tp_mult"] - 1)
            elif c["hit"] == "stop":
                pnl += stake * CFG["stop_pct"] / 100
            else:
                pnl += stake * (c["last"] / c["price"] - 1)
        out.append(f"Paper P&L if you took every resolved BUY at ${stake:.0f}: ${pnl:+.2f} "
                   f"(before fees/slippage)")
    out.append("Rule of thumb: don't trust this until there are 30+ resolved BUYs.")
    return "\n".join(out)


# ---------------------------------------------------------------- positions
def check_positions(state, held, pairs, rugs, owner=""):
    alerts = []
    pos = state["positions"]
    sol_price = None
    for mint in list(pos):
        if mint not in held:
            pos.pop(mint)  # sold (or moved)
    for mint, amt in held.items():
        p = pairs.get(mint)
        if not p:
            continue
        m = metrics(p)
        value = amt * m["price"]
        if value < 0.10:
            continue
        rk = rugs.get(mint)
        ps = pos.get(mint)
        if not ps:
            ps = pos[mint] = dict(entry=m["price"], peak=m["price"], since=NOW, tp=False, sent=[],
                                  creator_bal=(rk or {}).get("creator_bal"), sym=m["symbol"], src="seen")
        if ps.get("src") != "chain" and owner and ps.get("tries", 0) < 5:
            ps["tries"] = ps.get("tries", 0) + 1
            if sol_price is None:
                sol_price = sol_usd()
            real = entry_from_chain(owner, mint, sol_price) if sol_price else None
            if real:
                ps["entry"], ps["src"] = real, "chain"
                ps["peak"] = max(m["price"], real)
        if "announced" not in ps:
            ps["announced"] = True
            how = "from your buy transactions" if ps.get("src") == "chain" else "estimate: price when first seen"
            alerts.append(f"👀 Watching your <b>${html.escape(m['symbol'])}</b> ({usd(value)}, "
                          f"{(m['price'] / ps['entry'] - 1) * 100:+.0f}%). Entry {px(ps['entry'])} ({how}). "
                          f"Stop at {px(ps['entry'] * (1 + CFG['stop_pct'] / 100))}.")
        ps["peak"] = max(ps["peak"], m["price"])
        ps["value"] = value
        chg = (m["price"] / ps["entry"] - 1) * 100
        sig = None
        if rk and not rk["ok"]:
            sig = ("rug", f"🚨 <b>SELL NOW ${html.escape(m['symbol'])}</b>: rug check failed: "
                          f"{html.escape('; '.join(rk['fails']))}")
        elif rk and ps.get("creator_bal") and rk.get("creator_bal") is not None and \
                float(rk["creator_bal"]) < float(ps["creator_bal"]) * (1 - CFG["dev_sell_drop"]):
            drop = 1 - float(rk["creator_bal"]) / float(ps["creator_bal"])
            sig = ("dev", f"🚨 <b>SELL NOW ${html.escape(m['symbol'])}</b>: dev sold {drop:.0%} of their bag")
        elif chg <= CFG["stop_pct"]:
            sig = ("stop", f"🛑 <b>STOP-LOSS ${html.escape(m['symbol'])}</b>: {chg:+.0f}% from entry. Sell.")
        elif not ps["tp"] and m["price"] >= ps["entry"] * CFG["tp_mult"]:
            ps["tp"] = True
            sig = ("tp", f"💰 <b>TAKE PROFIT ${html.escape(m['symbol'])}</b>: {chg:+.0f}%. "
                         f"Sell at least ${CFG['stake_usd']:.0f} (your stake) now; ride the rest.")
        elif ps["tp"] and m["price"] <= ps["peak"] * (1 + CFG["trail_from_peak"] / 100):
            sig = ("trail", f"📉 <b>SELL ${html.escape(m['symbol'])}</b>: down "
                            f"{(1 - m['price'] / ps['peak']):.0%} from peak after your 2x.")
        elif m["b1"] + m["s1"] and m["b1"] / (m["b1"] + m["s1"]) < 0.35 and m["price"] < ps["peak"] * 0.8:
            sig = ("sellers", f"⚠️ <b>Consider selling ${html.escape(m['symbol'])}</b>: sellers dominating, "
                              f"{(1 - m['price'] / ps['peak']):.0%} off peak.")
        if sig and sig[0] not in ps["sent"]:
            ps["sent"].append(sig[0])
            alerts.append(sig[1] + f"\nNow {px(m['price'])} · value {usd(value)}\n{html.escape(m['url'])}")
    return alerts


# ---------------------------------------------------------------- main
def run():
    BAL.clear(); ACCTS.clear(); _SOLP.clear()
    state = load_state()
    cmds = read_commands(state)
    if not (os.environ.get("TELEGRAM_CHAT_ID") or state.get("chat_id")):
        # nobody to alert yet: don't scan, or alerts would be marked sent while going nowhere
        log("no chat yet: send /start to your bot in Telegram; skipping this run")
        save_state(state)  # keeps the update offset only
        return
    wallet = os.environ.get("WALLET", "").strip()

    order, tags = discover()
    held = wallet_tokens(wallet) if wallet else {}
    held_ok = held is not None
    if not held_ok:
        log("wallet RPC failed this run; keeping previous positions untouched")
        held = {}
    held_mints = list(held) if held_ok else list(state["positions"])
    extra = list(state["watch"]) + held_mints + [c["mint"] for c in state["calls"] if NOW - c["ts"] < 86400]
    pairs = pairs_for(order + extra)
    update_calls(state, {k: metrics(v)["price"] for k, v in pairs.items()})

    # gate cheaply on market data, then rug-check the best few
    judged, rugs, pending, live = [], {}, [], []
    for mint in order:
        if mint not in pairs:
            continue
        m = metrics(pairs[mint])
        gate, why = market_gate(m)
        if m["price"] > 0 and m["liq"] >= CFG["min_liq"] and m["b1"] + m["s1"] >= CFG["min_h1_txns"]:
            live.append((mint, m))
        if gate == "skip":
            continue
        if gate in ("AVOID", "WAIT"):
            judged.append((mint, m, None, dict(verdict=gate, score=0, reasons=why)))
            continue
        pending.append((mint, m))
    pending.sort(key=lambda x: -(x[1]["vol_h1"] / max(x[1]["liq"], 1)))
    must_check = [k for k in dict.fromkeys(held_mints + list(state["watch"])) if k in pairs]
    for mint in must_check:
        rugs[mint] = rugcheck(mint)
        time.sleep(0.3)
    for mint, m in pending[:CFG["max_rugchecks"]]:
        rk = rugs.get(mint) or rugcheck(mint)
        time.sleep(0.3)
        if rk is None:
            continue
        rugs[mint] = rk
        judged.append((mint, m, rk, judge(m, rk)))

    msgs = []
    # positions first: exits matter more than entries
    if held_ok:
        msgs += check_positions(state, held, pairs, rugs, wallet)
    holding = any(p.get("value", 0) >= 1 for p in state["positions"].values())
    bp = buying_power(wallet) if held_ok else None
    stake = CFG["stake_usd"]
    can_buy = (bp["usd"] >= stake) if bp else not holding

    def power_line():
        if not bp:
            return ""
        n = int(bp["usd"] // stake)
        return (f"Buying power {usd(bp['usd'])}: enough for {n} buy(s) at {usd(stake)}" if n else
                f"Buying power {usd(bp['usd'])}: below the {usd(stake)} minimum")

    buys = []  # (rank, mint, m, rk, j): your stake covers ONE position, so only the best gets a card
    # dip watch -> BUY when target reached and still safe
    for mint in list(state["watch"]):
        w = state["watch"][mint]
        if NOW - w["set"] > CFG["watch_hours"] * 3600:
            state["watch"].pop(mint)
            continue
        if mint in pairs and rugs.get(mint):
            m, rk = metrics(pairs[mint]), rugs[mint]
            if not rk["ok"]:
                state["watch"].pop(mint)
                continue
            if m["price"] <= w["target"] and can_buy:
                tot = m["b1"] + m["s1"]
                sc = momentum_score(m, rk)[0]
                if tot and m["b1"] / tot >= 0.45 and sc >= 50:
                    buys.append((sc + 10, mint, m, rk, dict(verdict="BUY", score=sc, reasons=[
                        f"dip target {px(w['target'])} reached and buyers still active"])))
                    state["watch"].pop(mint)

    recent = lambda mint: NOW - state["alerted"].get(mint, 0) < CFG["realert_hours"] * 3600
    for mint, m, rk, j in judged:
        if recent(mint):
            continue
        if j["verdict"] == "BUY":
            if can_buy:
                buys.append((j["score"], mint, m, rk, j))
        elif j["verdict"] == "WAIT" and j.get("target") and mint not in state["watch"]:
            state["watch"][mint] = dict(target=j["target"], set=NOW, sym=m["symbol"])
            msgs.append(card(mint, m, rk, j, tags.get(mint, [])))
            record_call(state, mint, m, j)
            state["alerted"][mint] = NOW
        elif j["verdict"] == "AVOID" and rk is not None:
            record_call(state, mint, m, j)  # logged silently so /stats can grade the rug filter
            state["alerted"][mint] = NOW
    buys.sort(key=lambda x: -x[0])
    for i, (_, mint, m, rk, j) in enumerate(buys):
        record_call(state, mint, m, j)  # every BUY is graded in /stats, even ones not sent
        state["alerted"][mint] = NOW
        if i == 0:
            j["odds"] = odds_line(state, j["score"])
            j["power"] = power_line()
            others = ", ".join("$" + html.escape(b[2]["symbol"]) for b in buys[1:4])
            msgs.append(card(mint, m, rk, j, tags.get(mint, [])) +
                        (f"\nAlso qualifying (weaker): {others}" if others else ""))
    state["alerted"] = {k: v for k, v in state["alerted"].items() if NOW - v < 86400}
    state["last_judged"] = [dict(mint=mi, sym=m["symbol"], verdict=j["verdict"], score=j["score"],
                                 why="; ".join(j["reasons"])[:90], price=m["price"])
                            for mi, m, rk, j in judged][:15]
    verdicts = {mi: j for mi, m, rk, j in judged}
    icon = {"BUY": "🟢", "WAIT": "🟡", "AVOID": "🔴"}

    def row(mi, m, extra=""):
        j = verdicts.get(mi)
        tag = f"{icon[j['verdict']]} {j['verdict']}" if j else "⚪ not checked"
        return (f"{tag} <b>${html.escape(m['symbol'])}</b> {px(m['price'])} · 1h {m['ch_h1']:+.0f}% · "
                f"MC {usd(m['mcap'])}{extra}")

    if not state.get("menu_set"):
        tg("setMyCommands", commands=[dict(command=c, description=d) for c, d in (
            ("buy", "What passes the checks and you can afford now"), ("trending", "Trending coins with verdicts"),
            ("gainers", "Biggest 1h movers (live, liquid)"), ("graduated", "Coins that just left pump.fun"),
            ("balance", "Your buying power and positions"), ("positions", "Your open coins vs entry"),
            ("stats", "How the bot's calls have done"))])
        state["menu_set"] = True

    for c in dict.fromkeys(cmds):
        if c == "/buy":
            bl = [(mi, m) for mi, m, rk, j in judged if j["verdict"] == "BUY"]
            body = "\n".join(row(mi, m, f" · score {verdicts[mi]['score']}") for mi, m in bl) or \
                "Nothing passes the rug checks with buying momentum right now."
            pl = power_line()
            msgs.append(f"<b>Passing checks now</b>\n{body}" + (f"\n\n{pl}" if pl else "") +
                        "\nUnproven signals: see /stats before trusting them.")
        elif c == "/gainers":
            top = sorted(live, key=lambda x: -x[1]["ch_h1"])[:10]
            msgs.append("<b>Top 1h gainers</b> (liquid, active)\n" +
                        ("\n".join(row(mi, m) for mi, m in top) or "None right now.") +
                        "\nBig gainers are often the riskiest entries: check the verdict.")
        elif c == "/graduated":
            gl = [(mi, m) for mi, m in ((mi, metrics(pairs[mi])) for mi in order if mi in pairs)
                  if any("graduated" in t for t in tags.get(mi, []))]
            gl.sort(key=lambda x: (x[1]["age_min"] if x[1]["age_min"] is not None else 1e9))
            msgs.append("<b>Graduated to PumpSwap</b> (newest first)\n" + ("\n".join(
                row(mi, m, f" · age {m['age_min'] / 60:.1f}h" if m["age_min"] is not None else "")
                for mi, m in gl[:10]) or "None found this run."))
        elif c == "/balance":
            if not bp:
                msgs.append("Couldn't read your wallet balance this run (free RPC hiccup). Try again.")
            else:
                posv = sum(p.get("value", 0) for p in state["positions"].values())
                msgs.append(f"<b>Balance</b>\nStablecoins {usd(bp['stable'])} · SOL {bp['sol']:.4f} "
                            f"({usd(bp['sol_usd'])})\nIn coins: {usd(posv)} across {len(state['positions'])} "
                            f"position(s)\n{power_line()}")
        elif c in ("/scan", "/trending", "/start", "/help"):
            rows = sorted(state["last_judged"], key=lambda r: ({"BUY": 0, "WAIT": 1, "AVOID": 2}[r["verdict"]], -r["score"]))
            body = "\n".join(f"{icon[r['verdict']]} <b>${html.escape(r['sym'])}</b> {r['verdict']} {r['score']} · "
                             f"{html.escape(r['why'])}" for r in rows) or "Nothing passed the market filters this run."
            note = "" if can_buy else f"\n\n(BUY alerts paused: buying power under {usd(stake)}.)"
            msgs.append(f"<b>Trending scan</b>\n{body}{note}\n\nCommands: /buy /trending /gainers /graduated "
                        f"/balance /positions /stats")
        elif c == "/positions":
            if not state["positions"]:
                msgs.append("No open positions seen in your wallet.")
            for mint, ps in state["positions"].items():
                p = pairs.get(mint)
                if p:
                    m = metrics(p)
                    msgs.append(f"<b>${html.escape(ps['sym'])}</b> {(m['price'] / ps['entry'] - 1) * 100:+.0f}% "
                                f"· value {usd(ps.get('value', 0))} · stop {px(ps['entry'] * (1 + CFG['stop_pct'] / 100))}")
        elif c == "/stats":
            msgs.append(stats_text(state))

    for t in msgs:
        send(state, t)
        time.sleep(0.4)
    save_state(state)
    log(f"run ok: {len(order)} discovered, {len(judged)} judged, {len(rugs)} rug-checked, "
        f"{len(msgs)} messages, positions={len(state['positions'])}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log(f"FATAL {type(e).__name__}: {e}")
        sys.exit(1)
