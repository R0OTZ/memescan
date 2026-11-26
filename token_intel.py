"""Token analysis engine shared by /best, /token, /rugcheck and /watch.

collect() gathers raw data from the providers (each with its own source and fetch time), analyze() turns it into
features, findings and scores, and the fmt_* functions render Telegram messages. Nothing here invents data: a
missing source makes its category 'unavailable', is excluded from the score, and is listed in the report.
"""
import html
import json
import math
import os
import time
from datetime import datetime

import providers as P

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    from datetime import timezone
    TZ = timezone.utc

DEFAULT_WEIGHTS = dict(momentum=20, activity=15, holders=15, wallets=15, social=15, safety=15, lifecycle=5)
CAT_NAMES = dict(momentum="Market momentum", activity="Liquidity & trading activity", holders="Holder distribution",
                 wallets="Wallet intelligence", social="X / social momentum", safety="Token safety",
                 lifecycle="Graduation & lifecycle")
BURN_OWNERS = {"11111111111111111111111111111111", "1nc1nerator11111111111111111111111111111111"}
SEV_POINTS = dict(critical=40, high=20, medium=10, low=4)
RISK_BANDS = [(80, "Critical risk indicators"), (60, "High risk"), (40, "Elevated risk"), (20, "Some concerns"),
              (0, "Lower detected risk")]
VERDICTS = ("Strong evidence of an interesting opportunity", "Worth monitoring", "High uncertainty",
            "Avoid due to identified risks")


def load_weights(path=None):
    """Weights from env MEMESCAN_WEIGHTS (JSON) or a weights.json file; unknown keys and negatives ignored."""
    w = dict(DEFAULT_WEIGHTS)
    raw = os.environ.get("MEMESCAN_WEIGHTS")
    if not raw and path and os.path.exists(path):
        try:
            raw = open(path).read()
        except OSError:
            raw = None
    try:
        for k, v in (json.loads(raw) if raw else {}).items():
            if k in w and isinstance(v, (int, float)) and v >= 0:
                w[k] = float(v)
    except (ValueError, AttributeError):
        pass
    return w


def esc(s):
    return html.escape(str(s))


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def usd(x):
    if x is None:
        return "n/a"
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}k" if a >= 1e3 \
        else f"${a:,.2f}"
    return ("-" if x < 0 else "") + s


def px(x):
    if not x:
        return "$0"
    if x >= 1:
        return f"${x:,.4f}"
    s = f"{x:.12f}".rstrip("0")
    return "$" + f"{x:.{max(4, len(s) - len(s.lstrip('0.')) + 2)}f}"


def pct(x, signed=True):
    return "n/a" if x is None else (f"{x:+.1f}%" if signed else f"{x:.1f}%")


def clock(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%I:%M:%S %p ET").lstrip("0")


def hm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%I:%M %p").lstrip("0")


def when(ts):
    d = datetime.fromtimestamp(ts, TZ)
    today = datetime.now(TZ).date()
    return clock(ts) if d.date() == today else d.strftime("%b %d ") + clock(ts)


def ago(sec):
    if sec is None:
        return "?"
    sec = int(sec)
    return f"{sec}s" if sec < 90 else f"{sec // 60}m" if sec < 5400 else f"{sec // 3600}h"


def meta(r):
    return dict(ok=bool(r and r.get("ok")), fetched=(r or {}).get("fetched"), error=(r or {}).get("error"),
                cached=bool((r or {}).get("cached")))


# ---------------------------------------------------------------- history
class History:
    """Compact per-token snapshots so analyses can compare against the past (15m change, holder trends)."""
    FIELDS = ("ts", "price", "mcap", "liq", "vol_h1", "tx_h1", "holders", "top10", "insiders", "dev_pct", "clusters")

    def __init__(self, path=None, keep_sec=6 * 3600, max_tokens=400, gap=55):
        self.path, self.keep, self.max, self.gap = path, keep_sec, max_tokens, gap
        self.d = {}
        if path:
            try:
                with open(path) as f:
                    self.d = json.load(f)
            except Exception:
                self.d = {}

    def get(self, mint):
        return [dict(zip(self.FIELDS, r)) for r in self.d.get(mint, [])]

    def add(self, mint, a):
        mk, hd = a.get("market") or {}, a.get("holders") or {}
        if not mk:
            return
        rows = [r for r in self.d.get(mint, []) if a["t"] - r[0] < self.keep]
        if rows and a["t"] - rows[-1][0] < self.gap:
            return
        rows.append([round(a["t"]), mk.get("price"), mk.get("mcap"), mk.get("liq"), mk.get("vol_h1"),
                     mk.get("tx_h1"), hd.get("total"), hd.get("top10"), hd.get("insider_pct"), hd.get("dev_pct"),
                     hd.get("cluster_pct")])
        self.d[mint] = rows

    def save(self):
        if not self.path:
            return
        latest = sorted(self.d, key=lambda k: -(self.d[k][-1][0] if self.d[k] else 0))[:self.max]
        self.d = {k: self.d[k] for k in latest}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.d, f, separators=(",", ":"))
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass


def sample_near(hist, now, sec_ago, tol):
    best = None
    for h in hist:
        d = abs((now - h["ts"]) - sec_ago)
        if d <= tol and (best is None or d < best[0]):
            best = (d, h)
    return best[1] if best else None


# ---------------------------------------------------------------- collect
def collect(mint, cache=None, deep=False, ds=None, fomo=None, trades=True, route=True):
    """Raw inputs for one token. ds: a DexScreener Res covering this mint (from a batch) to avoid refetching.
    deep: also pull the last 300 trades (GeckoTerminal) and a Jupiter sell quote."""
    t = time.time()
    src = {}
    if ds is None or mint not in (ds.get("data") or {}):
        ds = P.ds_tokens([mint], cache)
    src["DexScreener"] = meta(ds)
    pairs = (ds.get("data") or {}).get(mint, [])
    rc = P.rugcheck(mint, cache)
    src["RugCheck"] = meta(rc)
    ma = P.mint_account(mint, cache)
    src["Solana RPC"] = meta(ma)
    if fomo is None:
        fr = P.fomo_feed(cache)
        src["fomo"] = meta(fr)
        fomo = fr.get("data") or {}
    b = dict(mint=mint, t=t, src=src, pairs=pairs, rc=rc["data"] if rc["ok"] else None,
             chain=ma["data"] if ma["ok"] else None, fomo=(fomo or {}).get(mint), trades=None, route=None)
    b["t"] = time.time()
    if deep and pairs and trades:
        bp = P.best_pair(pairs)
        tr = P.gt_trades(bp.get("pairAddress"), cache)
        src["GeckoTerminal trades"] = meta(tr)
        b["trades"] = tr["data"] if tr["ok"] else None
    if deep and route:
        dec = (b["chain"] or {}).get("decimals") or ((b["rc"] or {}).get("token") or {}).get("decimals")
        if dec is not None:
            jr = P.sell_route(mint, dec, cache)
            src["Jupiter"] = meta(jr)
            b["route"] = jr["data"] if jr["ok"] else None
    b["t"] = time.time()
    return b


# ---------------------------------------------------------------- features
def pair_metrics(p, now):
    def f(d, k):
        try:
            return float((p.get(d) or {}).get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    tx = p.get("txns") or {}

    def bs(w):
        x = tx.get(w) or {}
        return int(x.get("buys") or 0), int(x.get("sells") or 0)
    b5, s5 = bs("m5")
    b1, s1 = bs("h1")
    b6, s6 = bs("h6")
    b24, s24 = bs("h24")
    created = p.get("pairCreatedAt")
    return dict(
        symbol=(p.get("baseToken") or {}).get("symbol") or "?", name=(p.get("baseToken") or {}).get("name") or "",
        price=float(p.get("priceUsd") or 0), liq=f("liquidity", "usd"),
        mcap=float(p.get("marketCap") or p.get("fdv") or 0), fdv=float(p.get("fdv") or 0),
        vol_m5=f("volume", "m5"), vol_h1=f("volume", "h1"), vol_h6=f("volume", "h6"), vol_h24=f("volume", "h24"),
        ch_m5=f("priceChange", "m5"), ch_h1=f("priceChange", "h1"), ch_h6=f("priceChange", "h6"),
        ch_h24=f("priceChange", "h24"), b5=b5, s5=s5, b1=b1, s1=s1, b6=b6, s6=s6, b24=b24, s24=s24,
        tx_m5=b5 + s5, tx_h1=b1 + s1, pair=p.get("pairAddress") or "", dex=p.get("dexId") or "?",
        url=p.get("url") or "", age_h=(now * 1000 - created) / 3.6e6 if created else None,
        socials=[(s.get("type"), s.get("url")) for s in ((p.get("info") or {}).get("socials") or [])],
        websites=[w.get("url") for w in ((p.get("info") or {}).get("websites") or [])],
        boosts=int(((p.get("boosts") or {}).get("active")) or 0))


def market_features(b, hist):
    p = P.best_pair(b["pairs"])
    if not p:
        return None
    now = b["t"]
    m = pair_metrics(p, now)
    m["n_pairs"] = len(b["pairs"])
    m["dexes"] = sorted({x.get("dexId") or "?" for x in b["pairs"]})
    m["liq_total"] = sum(float((x.get("liquidity") or {}).get("usd") or 0) for x in b["pairs"])
    pump = b["mint"].endswith("pump")
    if m["dex"] == "pumpfun":
        m["graduation"] = "on the pump.fun bonding curve (not graduated)"
        m["graduated"] = False
    elif pump:
        m["graduation"] = f"graduated from pump.fun, now on {m['dex']}"
        m["graduated"] = True
    else:
        m["graduation"] = f"not a pump.fun launch; trades on {m['dex']}"
        m["graduated"] = None
    m["br_m5"] = m["b5"] / m["tx_m5"] if m["tx_m5"] else None
    m["br_h1"] = m["b1"] / m["tx_h1"] if m["tx_h1"] else None
    m["accel"] = (m["vol_m5"] * 12 / m["vol_h1"]) if m["vol_h1"] > 0 else None  # last 5m pace vs last hour
    m["trend6"] = (m["vol_h1"] / (m["vol_h6"] / 6)) if m["vol_h6"] > 0 else None  # last hour vs 6h hourly avg
    m["avg_trade_h1"] = m["vol_h1"] / m["tx_h1"] if m["tx_h1"] else None
    m["mcap_liq"] = m["mcap"] / m["liq"] if m["liq"] else None
    m["turnover"] = m["vol_h1"] / m["liq"] if m["liq"] else None
    s15 = sample_near(hist, now, 900, 300)
    m["ch_m15"] = ((m["price"] / s15["price"] - 1) * 100) if s15 and s15.get("price") else None
    m["vol_m15_note"] = None if s15 else "no 15m source: needs ~15 min of the bot's own samples"
    s60 = sample_near(hist, now, 3600, 900)
    m["liq_chg_1h"] = ((m["liq"] / s60["liq"] - 1) * 100) if s60 and s60.get("liq") else None
    m["tx_chg"] = ((m["tx_h1"] / s60["tx_h1"] - 1) * 100) if s60 and s60.get("tx_h1") else None
    fm = b.get("fomo")
    if fm:
        m["fomo"] = fm
    return m


def holder_features(b, hist):
    rc = b.get("rc")
    if not rc:
        return None
    tok = rc.get("token") or {}
    supply = int(tok.get("supply") or (b.get("chain") or {}).get("supply") or 0)
    pools = set()
    for k, v in (rc.get("knownAccounts") or {}).items():
        if (v or {}).get("type") in ("AMM", "LOCKER"):
            pools.add(k)
    lp_locked = []
    for mk in rc.get("markets") or []:
        for key in ("pubkey", "liquidityA", "liquidityB"):
            if isinstance(mk.get(key), str):
                pools.add(mk[key])
        lp = mk.get("lp") or {}
        if lp.get("lpLockedPct") is not None:
            lp_locked.append((float(lp["lpLockedPct"]), mk.get("marketType") or "?", float(lp.get("lpLockedUSD") or 0)))
    for p in b.get("pairs") or []:
        if p.get("pairAddress"):
            pools.add(p["pairAddress"])
    raw = rc.get("topHolders") or []
    pcts = [float(h.get("pct") or 0) for h in raw]
    scale = 100.0 if pcts and max(pcts) <= 1.0 else 1.0
    top, burned, pooled = [], 0.0, 0.0
    for h in raw:
        p_ = float(h.get("pct") or 0) * scale
        if h.get("owner") in BURN_OWNERS:
            burned += p_
        elif h.get("owner") in pools or h.get("address") in pools:
            pooled += p_
        else:
            top.append(dict(owner=h.get("owner"), pct=p_, insider=bool(h.get("insider"))))
    top.sort(key=lambda x: -x["pct"])
    creator = rc.get("creator")
    cb = rc.get("creatorBalance")
    dev_pct = (float(cb) / supply * 100) if (cb is not None and supply) else None
    nets = []
    for n in rc.get("insiderNetworks") or []:
        amt = n.get("currentHolding", n.get("tokenAmount")) or 0
        nets.append(dict(id=n.get("id"), size=int(n.get("size") or 0), type=n.get("type"),
                         active=int(n.get("activeAccounts") or 0),
                         pct=(float(amt) / supply * 100) if supply else None))
    nets.sort(key=lambda x: -(x["pct"] or 0))
    out = dict(
        total=rc.get("totalHolders"), top=top[:20], top10=sum(x["pct"] for x in top[:10]),
        top20=sum(x["pct"] for x in top[:20]), largest=top[0]["pct"] if top else 0.0,
        insider_pct=sum(x["pct"] for x in top if x["insider"]), graph_insiders=int(rc.get("graphInsidersDetected") or 0),
        networks=nets, cluster_pct=sum(n["pct"] or 0 for n in nets), creator=creator, dev_pct=dev_pct,
        burned_pct=burned, pooled_pct=pooled, lp_locked=lp_locked, supply=supply,
        decimals=int(tok.get("decimals") or 0))
    old = sample_near(hist, b["t"], 3600, 1800)
    out["holders_chg"] = ((out["total"] / old["holders"] - 1) * 100) if old and old.get("holders") and out["total"] \
        else None
    out["top10_chg"] = (out["top10"] - old["top10"]) if old and old.get("top10") is not None else None
    out["trend_window"] = ago(b["t"] - old["ts"]) if old else None
    fm = b.get("fomo") or {}
    if out["total"] is None and fm.get("holders"):
        out["total"] = fm["holders"]
    return out


def trade_features(trades, liq, now):
    """Unique wallets, flow and manipulation patterns from the last ~300 trades."""
    if not trades:
        return None
    tr = sorted(trades, key=lambda x: x["ts"])
    span = max(1.0, tr[-1]["ts"] - tr[0]["ts"])
    buys = [x for x in tr if x["kind"] == "buy"]
    sells = [x for x in tr if x["kind"] == "sell"]
    bw = {x["wallet"] for x in buys if x["wallet"]}
    sw = {x["wallet"] for x in sells if x["wallet"]}
    vol = sum(x["usd"] for x in tr)
    per = {}
    for x in tr:
        d = per.setdefault(x["wallet"], dict(n=0, b=0, s=0, bu=0.0, su=0.0))
        d["n"] += 1
        if x["kind"] == "buy":
            d["b"] += 1
            d["bu"] += x["usd"]
        else:
            d["s"] += 1
            d["su"] += x["usd"]
    top_w = max(per.items(), key=lambda kv: kv[1]["bu"] + kv[1]["su"]) if per else (None, None)
    top_share = ((top_w[1]["bu"] + top_w[1]["su"]) / vol) if vol and top_w[1] else 0.0
    # wash: wallets that both buy and sell repeatedly with near-equal size
    wash = [w for w, d in per.items() if d["b"] >= 3 and d["s"] >= 3 and
            min(d["bu"], d["su"]) / max(d["bu"], d["su"], 1e-9) > 0.8]
    wash_vol = sum(per[w]["bu"] + per[w]["su"] for w in wash)
    big = max(250.0, 0.005 * (liq or 0))
    bigs = [x for x in tr if x["usd"] >= big]
    # coordinated selling: 3+ distinct wallets each selling big within 5 minutes
    coord = None
    bs_ = [x for x in bigs if x["kind"] == "sell"]
    for i, x in enumerate(bs_):
        win = [y for y in bs_[i:] if y["ts"] - x["ts"] <= 300]
        ws = {y["wallet"] for y in win}
        if len(ws) >= 3:
            coord = dict(wallets=len(ws), usd=sum(y["usd"] for y in win), ts=x["ts"])
            break
    recent = [x for x in tr if now - x["ts"] <= 900]
    return dict(
        n=len(tr), span_min=span / 60, unique_buyers=len(bw), unique_sellers=len(sw), wallets=len(per),
        buy_usd=sum(x["usd"] for x in buys), sell_usd=sum(x["usd"] for x in sells), vol=vol,
        net_usd=sum(x["usd"] for x in buys) - sum(x["usd"] for x in sells),
        top_wallet=top_w[0], top_share=top_share, repeat_wallets=sum(1 for d in per.values() if d["n"] >= 5),
        wash_wallets=len(wash), wash_share=(wash_vol / vol) if vol else 0.0,
        anomalous=sum(1 for x in tr if x.get("anomalous")), big_threshold=big,
        big_buys=sum(1 for x in bigs if x["kind"] == "buy"), big_sells=len(bs_),
        big_buy_usd=sum(x["usd"] for x in bigs if x["kind"] == "buy"),
        big_sell_usd=sum(x["usd"] for x in bs_), coordinated_sell=coord,
        recent_buy_usd=sum(x["usd"] for x in recent if x["kind"] == "buy"),
        recent_sell_usd=sum(x["usd"] for x in recent if x["kind"] == "sell"),
        newest_ts=tr[-1]["ts"])


def F(sev, status, text, evidence="", source=""):
    return dict(sev=sev, status=status, text=text, evidence=evidence, source=source)


def security_findings(b, mk, hd, tf):
    """Every check produces a finding: a problem (critical..low), a verified safe property ('ok'/'verified'),
    a check that found nothing ('ok'/'no issue'), or 'unknown' when the data wasn't available."""
    out = []
    ch, rc = b.get("chain"), b.get("rc")
    # authorities: on-chain first, RugCheck second
    if ch:
        for key, label, why in (("mint_authority", "Mint authority", "the holder can print unlimited new tokens"),
                                ("freeze_authority", "Freeze authority", "the holder can freeze your tokens so you can't sell")):
            if ch.get(key):
                out.append(F("critical", "verified", f"{label} is ACTIVE: {why}", f"authority {ch[key]}", "Solana RPC"))
            else:
                out.append(F("ok", "verified", f"{label} revoked", "mint account shows no authority", "Solana RPC"))
        ext = ch.get("extensions") or {}
        bad = dict(permanentDelegate=("critical", "permanent delegate: someone can move or burn anyone's tokens"),
                   transferHook=("high", "transfer hook: custom code runs on every transfer and can block sells"),
                   nonTransferable=("critical", "token is non-transferable"),
                   pausableConfig=("critical", "transfers can be paused"),
                   defaultAccountState=("high", "new accounts may start frozen"),
                   mintCloseAuthority=("medium", "mint can be closed"))
        for k, (sev, txt) in bad.items():
            st = ext.get(k)
            if st and not (k == "defaultAccountState" and str((st or {}).get("accountState", "")).lower() != "frozen"):
                if isinstance(st, dict) and k in ("permanentDelegate", "transferHook", "mintCloseAuthority") and \
                        not any(v for v in st.values()):
                    continue  # extension present but with no authority/program set
                out.append(F(sev, "verified", "Token-2022 " + txt, f"extension {k}", "Solana RPC"))
        fee = ext.get("transferFeeConfig")
        if fee:
            bps = ((fee.get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0)
            if bps:
                out.append(F("high" if bps >= 500 else "medium", "verified", f"transfer tax {bps / 100:.1f}% on every transfer",
                             "transferFeeConfig", "Solana RPC"))
        out.append(F("ok", "verified", f"token program: {'Token-2022' if ch.get('program', '').startswith('Tokenz') else 'SPL Token'}",
                     "", "Solana RPC"))
    elif rc:
        for key, label in (("mintAuthority", "Mint authority"), ("freezeAuthority", "Freeze authority")):
            if rc.get(key):
                out.append(F("critical", "reported", f"{label} is ACTIVE", f"authority {rc[key]}", "RugCheck"))
            else:
                out.append(F("ok", "reported", f"{label} revoked (RugCheck; on-chain check unavailable)", "", "RugCheck"))
    else:
        out.append(F("unknown", "unknown", "Mint/freeze authority: could not check (RPC and RugCheck unavailable)"))
    if rc:
        if rc.get("rugged"):
            out.append(F("critical", "reported", "RugCheck marks this token as RUGGED", "", "RugCheck"))
        tm = rc.get("tokenMeta") or {}
        if tm.get("mutable"):
            out.append(F("low", "reported", "metadata is mutable (name/image can be changed)", "", "RugCheck"))
        dangers = [r for r in rc.get("risks") or [] if (r.get("level") or "").lower() == "danger"]
        warns = [r for r in rc.get("risks") or [] if (r.get("level") or "").lower() == "warn"]
        for r in dangers[:4]:
            out.append(F("high", "reported", f"RugCheck danger: {r.get('name')}", (r.get("description") or "")[:120], "RugCheck"))
        for r in warns[:4]:
            out.append(F("low", "reported", f"RugCheck warning: {r.get('name')}", (r.get("description") or "")[:120], "RugCheck"))
        if not dangers and not warns and not rc.get("rugged"):
            out.append(F("ok", "no issue", "RugCheck lists no risk flags", "absence of flags is not proof of safety", "RugCheck"))
    else:
        out.append(F("unknown", "unknown", "RugCheck report unavailable: holder, LP and scam checks not run"))
    # liquidity
    if mk:
        if mk["liq"] < 5000:
            out.append(F("critical", "detected", f"liquidity only {usd(mk['liq'])}: trivial to drain, huge slippage", "", "DexScreener"))
        elif mk["liq"] < 15000:
            out.append(F("high", "detected", f"thin liquidity {usd(mk['liq'])}", "", "DexScreener"))
        if mk.get("liq_chg_1h") is not None and mk["liq_chg_1h"] <= -30:
            out.append(F("high", "detected", f"liquidity fell {mk['liq_chg_1h']:.0f}% in the last hour",
                         "compared with the bot's own snapshot", "DexScreener + history"))
        if mk.get("mcap_liq") and mk["mcap_liq"] > 40:
            out.append(F("medium", "detected", f"market cap is {mk['mcap_liq']:.0f}x liquidity: price can fall fast on sells", "", "DexScreener"))
    else:
        out.append(F("unknown", "unknown", "No trading pair found on DexScreener: liquidity unknown"))
    if hd:
        locks = hd["lp_locked"]
        if locks:
            best = max(locks)
            if mk and mk.get("dex") == "pumpfun":
                pass  # bonding curve: there is no LP to lock yet
            elif best[0] >= 95:
                out.append(F("ok", "reported", f"LP {best[0]:.0f}% locked/burned ({best[1]})", f"{usd(best[2])} locked", "RugCheck"))
            elif best[0] >= 50:
                out.append(F("medium", "reported", f"only {best[0]:.0f}% of LP locked/burned", "", "RugCheck"))
            else:
                out.append(F("high", "reported", f"LP mostly unlocked ({best[0]:.0f}% locked): liquidity can be pulled", "", "RugCheck"))
        else:
            out.append(F("unknown", "unknown", "LP lock/burn status not reported"))
        if hd["top10"] > 50:
            out.append(F("high", "detected", f"top 10 wallets hold {hd['top10']:.1f}% (pools excluded)", "", "RugCheck"))
        elif hd["top10"] > 30:
            out.append(F("medium", "detected", f"top 10 wallets hold {hd['top10']:.1f}% (pools excluded)", "", "RugCheck"))
        else:
            out.append(F("ok", "detected", f"top 10 wallets hold {hd['top10']:.1f}% (pools excluded)", "", "RugCheck"))
        if hd["largest"] > 15:
            out.append(F("high", "detected", f"one wallet holds {hd['largest']:.1f}%", hd["top"][0]["owner"], "RugCheck"))
        if hd["dev_pct"] is not None:
            if hd["dev_pct"] > 10:
                out.append(F("high", "detected", f"deployer still holds {hd['dev_pct']:.1f}%", hd["creator"] or "", "RugCheck"))
            elif hd["dev_pct"] > 3:
                out.append(F("medium", "detected", f"deployer holds {hd['dev_pct']:.1f}%", hd["creator"] or "", "RugCheck"))
            else:
                out.append(F("ok", "detected", f"deployer holds {hd['dev_pct']:.1f}%", "", "RugCheck"))
        if hd["insider_pct"] >= 20:
            out.append(F("high", "detected", f"wallets flagged as insiders hold {hd['insider_pct']:.1f}%", "", "RugCheck"))
        elif hd["insider_pct"] >= 10:
            out.append(F("medium", "detected", f"wallets flagged as insiders hold {hd['insider_pct']:.1f}%", "", "RugCheck"))
        big_net = [n for n in hd["networks"] if (n["pct"] or 0) >= 5]
        if hd["cluster_pct"] >= 25:
            out.append(F("high", "detected", f"{len(hd['networks'])} linked-wallet clusters hold {hd['cluster_pct']:.1f}%",
                         "; ".join(f"{n['size']} wallets {n['pct']:.1f}% ({n['type']})" for n in hd["networks"][:3]),
                         "RugCheck insider graph"))
        elif big_net:
            out.append(F("medium", "detected", f"linked-wallet clusters hold {hd['cluster_pct']:.1f}%",
                         "; ".join(f"{n['size']} wallets {n['pct']:.1f}%" for n in big_net[:3]), "RugCheck insider graph"))
        elif hd["networks"]:
            out.append(F("low", "detected", f"small linked-wallet clusters ({hd['cluster_pct']:.1f}% total)", "", "RugCheck insider graph"))
        else:
            out.append(F("ok", "no issue", "no linked-wallet clusters detected", "", "RugCheck insider graph"))
    if tf:
        if tf["wash_share"] >= 0.3:
            out.append(F("high", "detected", f"{tf['wash_share']:.0%} of recent volume is wallets trading back and forth (wash pattern)",
                         f"{tf['wash_wallets']} wallets, last {tf['n']} trades", "GeckoTerminal"))
        elif tf["wash_share"] >= 0.15:
            out.append(F("medium", "detected", f"{tf['wash_share']:.0%} of recent volume looks like wash trading", "", "GeckoTerminal"))
        if tf["top_share"] >= 0.35 and tf["n"] >= 50:
            out.append(F("medium", "detected", f"one wallet made {tf['top_share']:.0%} of recent volume", tf["top_wallet"] or "", "GeckoTerminal"))
        if tf["coordinated_sell"]:
            c = tf["coordinated_sell"]
            out.append(F("high", "detected", f"coordinated selling: {c['wallets']} wallets sold {usd(c['usd'])} within 5 min",
                         clock(c["ts"]), "GeckoTerminal"))
        if tf["anomalous"]:
            out.append(F("low", "detected", f"{tf['anomalous']} trades flagged anomalous by GeckoTerminal", "", "GeckoTerminal"))
    rt = b.get("route")
    if rt is not None:
        if rt.get("route"):
            out.append(F("ok", "detected", "Jupiter finds a sell route (sellability evidence, not a guarantee)",
                         ", ".join(x for x in rt.get("dexes") or [] if x), "Jupiter"))
        else:
            out.append(F("critical", "detected", "NO sell route found on Jupiter: possible honeypot", "", "Jupiter"))
    elif "Jupiter" in b["src"]:
        out.append(F("unknown", "unknown", "Sellability (Jupiter quote) check failed"))
    return out


def risk_score(findings, have_security):
    if not have_security:
        return None
    pts = sum(SEV_POINTS.get(f["sev"], 0) for f in findings)
    return int(min(100, pts))


def risk_band(r):
    if r is None:
        return "Cannot assess (security sources unavailable)"
    for lo, name in RISK_BANDS:
        if r >= lo:
            return name
    return RISK_BANDS[-1][1]


# ---------------------------------------------------------------- scoring
def category_scores(mk, hd, tf, risk, b):
    s = {}
    if mk:
        br = mk["br_h1"] if mk["br_h1"] is not None else 0.5
        v = 50 + (br - 0.5) * 150 + clamp(mk["ch_m5"], -10, 10)
        h1 = mk["ch_h1"]
        v += 10 if 0 < h1 <= 60 else 0 if 60 < h1 <= 150 else -15 if h1 > 150 else -15 if h1 < -20 else -5
        if mk["accel"] is not None:
            v += 10 if mk["accel"] >= 1.5 else -10 if mk["accel"] < 0.5 else 0
        if mk.get("ch_m15") is not None:
            v += 5 if mk["ch_m15"] > 0 else -5
        if mk["ch_h24"] < -40:
            v -= 12  # bouncing off a crash: rebounds after a -40% day often fail
        s["momentum"] = clamp(v, 0, 100)
        liq_s = clamp((math.log10(max(mk["liq"], 1)) - 4) / 1.48)
        t = mk["turnover"] or 0
        turn_s = 1.0 if 0.2 <= t <= 3 else 0.3 if t > 10 else 0.6 if t > 3 else 0.1 if t < 0.05 else 0.5
        s["activity"] = 100 * (0.4 * liq_s + 0.3 * turn_s + 0.3 * clamp(mk["tx_h1"] / 300))
        age = mk.get("age_h")
        if mk.get("graduated") is False:
            s["lifecycle"] = 35.0
        elif age is None:
            s["lifecycle"] = 50.0
        else:
            s["lifecycle"] = 40.0 if age < 1 else 80.0 if age < 6 else 100.0 if age < 72 else 70.0 if age < 336 else 45.0
    if hd:
        t10 = hd["top10"]
        t10_s = 1.0 if t10 <= 15 else 1 - (t10 - 15) / 30 if t10 <= 30 else 0.5 - (t10 - 30) / 50 if t10 <= 50 else 0.0
        cnt = hd["total"] or 0
        cnt_s = clamp((math.log10(max(cnt, 1)) - 2) / 1.7)
        g = hd.get("holders_chg")
        g_s = 0.5 if g is None else clamp(0.5 + g / 10)
        v = 100 * (0.55 * clamp(t10_s) + 0.3 * cnt_s + 0.15 * g_s)
        if hd["largest"] > 10:
            v -= 10
        s["holders"] = clamp(v, 0, 100)
        w = 80 - 2 * hd["insider_pct"] - 1.2 * hd["cluster_pct"] - 3 * (hd["dev_pct"] or 0)
        w -= 10 if hd["graph_insiders"] > 20 else 0
        if tf:
            flow = tf["net_usd"] / tf["vol"] if tf["vol"] else 0
            w += 20 * clamp(flow, -1, 1)
            w += 5 if tf["unique_buyers"] > tf["unique_sellers"] * 1.2 else -5 if tf["unique_sellers"] > tf["unique_buyers"] * 1.2 else 0
            w -= 25 if tf["wash_share"] >= 0.3 else 10 if tf["wash_share"] >= 0.15 else 0
            w -= 20 if tf["coordinated_sell"] else 0
        s["wallets"] = clamp(w, 0, 100)
    elif tf:
        flow = tf["net_usd"] / tf["vol"] if tf["vol"] else 0
        s["wallets"] = clamp(60 + 25 * flow - (25 if tf["wash_share"] >= 0.3 else 0), 0, 100)
    if risk is not None:
        s["safety"] = 100.0 - risk
    s["social"] = None  # no free X data source: excluded from the score and reported as missing
    return s


def analyze(b, hist=None, weights=None, now=None):
    hist = hist or []
    weights = weights or DEFAULT_WEIGHTS
    now = now or b["t"]
    mk = market_features(b, hist)
    hd = holder_features(b, hist)
    tf = trade_features(b.get("trades"), mk["liq"] if mk else 0, now)
    findings = security_findings(b, mk, hd, tf)
    have_sec = bool(b.get("chain") or b.get("rc"))
    risk = risk_score(findings, have_sec)
    cats = category_scores(mk, hd, tf, risk, b)
    avail = {k: v for k, v in cats.items() if v is not None and weights.get(k, 0) > 0}
    wsum = sum(weights[k] for k in avail)
    total_w = sum(v for v in weights.values())
    coverage = wsum / total_w if total_w else 0.0
    score = sum(weights[k] * v for k, v in avail.items()) / wsum if wsum else 0.0
    adj, pos, neg = [], [], []
    ages = {k: now - v["fetched"] for k, v in b["src"].items() if v.get("ok") and v.get("fetched")}
    mk_age = ages.get("DexScreener")
    if mk_age is not None and mk_age > 120:
        adj.append((-10, f"market data is {ago(mk_age)} old"))
    if mk:
        if mk["tx_m5"] == 0:
            adj.append((-15, "no trades in the last 5 minutes"))
        if mk["trend6"] is not None and mk["trend6"] < 0.5 and mk["vol_h6"] > 5000:
            adj.append((-8, f"volume fading: last hour is {mk['trend6']:.1f}x its 6h average"))
        if mk["ch_h1"] > 120 or mk["ch_h24"] > 400:
            adj.append((-12, f"already up {mk['ch_h1']:+.0f}% 1h / {mk['ch_h24']:+.0f}% 24h: late entry risk"))
        if mk["liq"] < 15000:
            adj.append((-10, f"low liquidity {usd(mk['liq'])}"))
        if (0 < mk["ch_h1"] <= 60 and mk["ch_h6"] > 0 and (mk["br_h1"] or 0) >= 0.55 and
                (mk["trend6"] or 0) >= 1.0 and mk["tx_h1"] >= 100):
            adj.append((5, "steady organic-looking growth: rising price, buyers ahead, volume above its average"))
    if tf and tf["wash_share"] >= 0.3:
        adj.append((-15, "artificial-looking volume"))
    score = clamp(score + sum(a for a, _ in adj), 0, 100)
    crit = [f for f in findings if f["sev"] == "critical"]
    disq = bool(crit) or (risk is not None and risk >= 60)
    if disq:
        verdict = VERDICTS[3]
    elif coverage < 0.6 or not hd or risk is None:
        verdict = VERDICTS[2]
    elif score >= 70 and risk < 40 and mk and (mk["br_h1"] or 0) >= 0.5 and (cats.get("momentum") or 0) >= 55:
        verdict = VERDICTS[0]
    elif score >= 55:
        verdict = VERDICTS[1]
    else:
        verdict = VERDICTS[2]
    # plain-language reasons
    if mk:
        if mk["br_h1"] is not None:
            (pos if mk["br_h1"] >= 0.55 else neg if mk["br_h1"] < 0.45 else []).append(
                f"{mk['br_h1']:.0%} of last-hour trades are buys ({mk['b1']} buys / {mk['s1']} sells)")
        if mk["accel"] is not None and mk["accel"] >= 1.5:
            pos.append(f"volume accelerating: last 5 min running {mk['accel']:.1f}x the hourly pace")
        if mk["accel"] is not None and mk["accel"] < 0.5 and mk["vol_h1"] > 2000:
            neg.append(f"volume slowing: last 5 min at {mk['accel']:.1f}x the hourly pace")
        if mk["ch_h24"] < -40:
            neg.append(f"down {mk['ch_h24']:.0f}% over 24h: this may be a bounce after a crash")
        if mk["liq"] >= 50000:
            pos.append(f"healthy liquidity {usd(mk['liq'])}")
    if hd:
        if hd["top10"] <= 25:
            pos.append(f"spread-out holders: top 10 own {hd['top10']:.1f}%")
        if hd.get("holders_chg") is not None:
            (pos if hd["holders_chg"] > 1 else neg if hd["holders_chg"] < -1 else []).append(
                f"holders {hd['holders_chg']:+.1f}% over {hd['trend_window']}")
    if tf:
        if tf["net_usd"] > 0:
            pos.append(f"net buying {usd(tf['net_usd'])} over the last {tf['n']} trades ({tf['unique_buyers']} buyers vs {tf['unique_sellers']} sellers)")
        else:
            neg.append(f"net selling {usd(-tf['net_usd'])} over the last {tf['n']} trades ({tf['unique_buyers']} buyers vs {tf['unique_sellers']} sellers)")
    for f in findings:
        if f["sev"] in ("critical", "high", "medium"):
            neg.append(f["text"])
    for a, why in adj:
        (pos if a > 0 else neg).append(why)
    missing = [P.UNAVAILABLE["x"], P.UNAVAILABLE["bubblemaps"], P.UNAVAILABLE["thesis"]]
    for k, v in b["src"].items():
        if not v["ok"]:
            missing.append(f"{k}: unavailable ({v.get('error') or 'error'})")
    if "GeckoTerminal trades" not in b["src"]:
        missing.append("Unique buyers/sellers and wallet flow: not fetched in this view")
    if mk and mk.get("ch_m15") is None:
        missing.append("15m change: " + (mk["vol_m15_note"] or "n/a"))
    a = dict(mint=b["mint"], t=now, src=b["src"], market=mk, holders=hd, trades=tf, findings=findings, risk=risk,
             risk_band=risk_band(risk), cats=cats, weights=weights, coverage=coverage, score=round(score),
             adjust=adj, disqualified=disq, verdict=verdict, positives=list(dict.fromkeys(pos)),
             negatives=list(dict.fromkeys(neg)), missing=missing, route=b.get("route"))
    a["scenarios"] = scenarios(a)
    return a


def dump_impact(pct_supply, mcap, liq):
    """Constant-product estimate of the price drop if a holder sells pct_supply% into the pool."""
    if not mcap or not liq:
        return None
    r = liq / 2  # USD value of the token side of the pool
    q = mcap * pct_supply / 100
    return (1 - 1 / (1 + q / r) ** 2) * 100


def scenarios(a):
    mk, hd = a["market"], a["holders"]
    if not mk:
        return dict(bull="Not enough market data.", bear="Not enough market data.")
    mk = dict(mk, br_h1=mk["br_h1"] or 0)
    bull = [f"If buyers stay ahead (now {mk['br_h1']:.0%} of 1h trades) and liquidity holds above "
            f"{usd(mk['liq'] * 0.8)}, momentum can continue"]
    if mk["accel"] is not None and mk["accel"] >= 1.2:
        bull.append(f"volume is already speeding up ({mk['accel']:.1f}x)")
    if hd and hd.get("holders_chg") is not None and hd["holders_chg"] > 0:
        bull.append(f"holder count is growing ({hd['holders_chg']:+.1f}%)")
    bear = []
    if hd and hd["top"]:
        imp = dump_impact(hd["largest"], mk["mcap"], mk["liq"])
        if imp is not None:
            bear.append(f"if the largest wallet ({hd['largest']:.1f}%) sold into the pool at once, price could drop ~{imp:.0f}% (pool math)")
    if hd and hd["cluster_pct"] >= 5:
        imp = dump_impact(hd["cluster_pct"], mk["mcap"], mk["liq"])
        if imp is not None:
            bear.append(f"linked clusters ({hd['cluster_pct']:.1f}%) selling together: ~{imp:.0f}% drop")
    if mk["ch_h1"] > 60:
        bear.append(f"after {mk['ch_h1']:+.0f}% in an hour, a pullback is common")
    if not bear:
        bear.append(f"a liquidity pull or a fade in volume below {usd(mk['vol_h1'] * 0.5)}/h would weaken the setup")
    return dict(bull="; ".join(bull) + ".", bear="; ".join(bear).capitalize() + ".")


# ---------------------------------------------------------------- formatting
def src_line(a):
    parts = []
    for k, v in a["src"].items():
        if v["ok"]:
            age = a["t"] - v["fetched"]
            parts.append(f"{k} {ago(age)} ({P.freshness(age)}{', cached' if v['cached'] else ''})")
        else:
            parts.append(f"{k} ✗ {v.get('error') or 'unavailable'}")
    return "Sources: " + " · ".join(parts) + f"\nChecked {clock(a['t'])}"


def verdict_icon(v):
    return {VERDICTS[0]: "🟢", VERDICTS[1]: "🟡", VERDICTS[2]: "⚪", VERDICTS[3]: "🔴"}[v]


def social_line(mk):
    links = [u for t, u in (mk or {}).get("socials", []) if t == "twitter"]
    fx = ((mk or {}).get("fomo") or {}).get("x")
    if fx and fx not in links:
        links.append(fx)
    return ("X link listed: " + esc(links[0]) if links else "No X link listed") + \
        ". Mentions, sentiment and influencer activity: unavailable (no free X data)."


RISK_WORD = {"Lower detected risk": "low", "Some concerns": "some concerns", "Elevated risk": "elevated",
             "High risk": "high", "Critical risk indicators": "critical"}
SHORT_VERDICT = {VERDICTS[0]: "Strong setup", VERDICTS[1]: "Worth watching", VERDICTS[2]: "Unclear",
                 VERDICTS[3]: "Avoid"}


def kfmt(n):
    """159303 -> 159k, 1250 -> 1.3k"""
    if n is None:
        return "?"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def short_reasons(a):
    """Plain-language pros and cons, most important first."""
    mk, hd, tf = a["market"] or {}, a["holders"] or {}, a["trades"]
    pro, con = [], []
    br = mk.get("br_h1")
    if br is not None and br >= 0.55:
        pro.append("buyers in control")
    elif br is not None and br < 0.45:
        con.append("sellers in control")
    if (mk.get("accel") or 0) >= 1.5:
        pro.append("volume picking up")
    elif mk.get("accel") is not None and mk["accel"] < 0.5 and mk.get("vol_h1", 0) > 2000:
        con.append("volume fading")
    if mk.get("liq", 0) >= 50000:
        pro.append("deep liquidity")
    elif mk.get("liq", 0) < 15000:
        con.append("thin liquidity")
    if hd:
        if hd["top10"] <= 25:
            pro.append("holders spread out")
        elif hd["top10"] > 30:
            con.append(f"top 10 wallets own {hd['top10']:.0f}%")
        if (hd.get("holders_chg") or 0) > 1:
            pro.append("holder count growing")
        if (hd.get("dev_pct") or 0) > 3:
            con.append(f"dev still holds {hd['dev_pct']:.0f}%")
        if hd["insider_pct"] >= 10:
            con.append(f"insiders own {hd['insider_pct']:.0f}%")
    if tf:
        (pro if tf["net_usd"] > 0 else con).append("more money buying than selling" if tf["net_usd"] > 0
                                                    else "more money selling than buying")
        if tf["wash_share"] >= 0.15:
            con.append("fake-looking volume")
    if mk.get("ch_h24", 0) < -40:
        con.append(f"crashed {abs(mk['ch_h24']):.0f}% in 24h")
    if mk.get("ch_h1", 0) > 120 or mk.get("ch_h24", 0) > 400:
        con.append("already pumped a lot: late entry")
    if mk.get("tx_m5") == 0:
        con.append("no trades in the last 5 min")
    for f in a["findings"]:
        if "transfer tax" in f["text"]:
            con.append(f["text"].replace("transfer tax ", "").replace(" on every transfer", " tax on every trade"))
    return pro, con


def biggest_risk(a):
    mk, hd = a["market"] or {}, a["holders"] or {}
    crit = [f["text"] for f in a["findings"] if f["sev"] == "critical"]
    if crit:
        return crit[0]
    if hd and hd.get("cluster_pct", 0) >= 5:
        return f"linked wallets own {hd['cluster_pct']:.0f}%; if they sell together, price could crash"
    if hd and hd.get("largest", 0) >= 3:
        imp = dump_impact(hd["largest"], mk.get("mcap"), mk.get("liq"))
        return f"biggest wallet owns {hd['largest']:.0f}%" + (f"; selling it all could drop price ~{imp:.0f}%" if imp else "")
    if mk.get("ch_h1", 0) > 60:
        return f"up {mk['ch_h1']:.0f}% in an hour; pullbacks are common"
    return "liquidity could be pulled or buyers could fade"


def fmt_best_item(i, a):
    """Short /best card: one idea per line, plain words."""
    mk, hd, tf = a["market"], a["holders"], a["trades"]
    e = esc
    pro, con = short_reasons(a)
    sv = SHORT_VERDICT[a["verdict"]]
    L = [f"{verdict_icon(a['verdict'])} <b>#{i} ${e(mk['symbol'])}</b> · <b>{a['score']}/100</b> · {sv}",
         f"<code>{e(a['mint'])}</code>",
         f"💰 MC {usd(mk['mcap'])} · Liq {usd(mk['liq'])} · 1h {mk['ch_h1']:+.0f}% · 24h {mk['ch_h24']:+.0f}%",
         f"📊 1h: {mk['b1']} buys / {mk['s1']} sells · volume {usd(mk['vol_h1'])}"]
    if hd:
        L.append(f"👥 {kfmt(hd['total'])} holders · top 10 own {hd['top10']:.0f}%"
                 + (f" · dev {hd['dev_pct']:.0f}%" if hd["dev_pct"] is not None else ""))
    if a["risk"] is not None:
        L.append(f"🛡 Rug risk {a['risk']}/100: {RISK_WORD.get(a['risk_band'], a['risk_band'].lower())}")
    else:
        L.append("🛡 Safety: couldn't check (data source down)")
    if pro:
        L.append("✅ " + ", ".join(pro[:3]))
    if con:
        L.append("➖ " + ", ".join(con[:3]))
    L.append("⚠️ " + e(biggest_risk(a)))
    down = [k for k, v in a["src"].items() if not v["ok"]]
    stale = (a["t"] - (a["src"].get("DexScreener") or {}).get("fetched", a["t"])) > 120
    L.append(f"🕐 Checked {hm(a['t'])}" + (" · ⚠️ prices not fresh" if stale else "")
             + (f" · missing: {', '.join(down)}" if down else ""))
    return "\n".join(L)


def fmt_findings(findings, kinds):
    icon = dict(critical="⛔", high="🔴", medium="🟠", low="🟡", ok="✅", unknown="❔")
    return "\n".join(f"{icon[f['sev']]} {esc(f['text'])}" + (f" <i>[{esc(f['source'])}"
                     + (f": {esc(f['evidence'][:90])}" if f["evidence"] else "") + "]</i>" if f["source"] else "")
                     for f in findings if f["sev"] in kinds)


def fmt_token(a):
    """Full /token report, split into Telegram-sized messages."""
    mk, hd, tf = a["market"], a["holders"], a["trades"]
    e = esc
    if not mk:
        return [f"No trading pair found for <code>{e(a['mint'])}</code> on DexScreener.\n" +
                (f"Security: {a['risk_band']}" if a["risk"] is not None else "") + "\n" + src_line(a)]
    m1 = [f"🔎 <b>${e(mk['symbol'])}</b> {e(mk['name'])}",
          f"<code>{e(a['mint'])}</code>",
          "<b>A. Overview</b>",
          f"Price {px(mk['price'])} · MC {usd(mk['mcap'])} · FDV {usd(mk['fdv'])}",
          f"Liquidity {usd(mk['liq'])} (all pairs {usd(mk['liq_total'])}) · MC/liq {mk['mcap_liq'] or 0:.1f}x",
          f"Age {mk['age_h']:.1f}h" if mk["age_h"] is not None else "Age unknown",
          f"Status: {e(mk['graduation'])}",
          f"Pairs: {mk['n_pairs']} on {e(', '.join(mk['dexes']))}" + (f" · DexScreener boosts {mk['boosts']}" if mk["boosts"] else ""),
          "<b>B. Market behavior</b>",
          f"5m {pct(mk['ch_m5'])} · 15m {pct(mk['ch_m15'])} · 1h {pct(mk['ch_h1'])} · 6h {pct(mk['ch_h6'])} · 24h {pct(mk['ch_h24'])}",
          f"Volume 5m {usd(mk['vol_m5'])} · 1h {usd(mk['vol_h1'])} · 6h {usd(mk['vol_h6'])} · 24h {usd(mk['vol_h24'])}",
          f"Txns (buys/sells) 5m {mk['b5']}/{mk['s5']} · 1h {mk['b1']}/{mk['s1']} · 24h {mk['b24']}/{mk['s24']}"]
    if mk["accel"] is not None:
        state = "accelerating" if mk["accel"] >= 1.3 else "declining" if mk["accel"] < 0.7 else "steady"
        m1.append(f"Momentum: {state} (5m pace {mk['accel']:.1f}x the 1h pace"
                  + (f"; 1h is {mk['trend6']:.1f}x the 6h average)" if mk["trend6"] is not None else ")"))
    if tf:
        m1.append(f"Last {tf['n']} trades ({tf['span_min']:.0f} min): {tf['unique_buyers']} unique buyers, "
                  f"{tf['unique_sellers']} unique sellers, net {usd(tf['net_usd'])}")
        m1.append(f"Large trades (≥{usd(tf['big_threshold'])}): {tf['big_buys']} buys {usd(tf['big_buy_usd'])} / "
                  f"{tf['big_sells']} sells {usd(tf['big_sell_usd'])}")
        unusual = []
        if tf["wash_share"] >= 0.15:
            unusual.append(f"wash-like volume {tf['wash_share']:.0%}")
        if tf["top_share"] >= 0.25:
            unusual.append(f"one wallet = {tf['top_share']:.0%} of volume")
        if tf["repeat_wallets"]:
            unusual.append(f"{tf['repeat_wallets']} wallets traded 5+ times")
        if tf["coordinated_sell"]:
            unusual.append("coordinated sell burst")
        m1.append("Unusual activity: " + (", ".join(unusual) or "none detected"))
    else:
        m1.append("Unique buyers/sellers: unavailable (GeckoTerminal trades not reachable)")
    m2 = ["<b>C. Holders</b>"]
    if hd:
        m2.append(f"Holders {hd['total'] or '?'}" + (f" ({hd['holders_chg']:+.1f}% over {hd['trend_window']})"
                                                     if hd.get("holders_chg") is not None else " (trend after ~1h of bot history)"))
        m2.append(f"Top 10 {hd['top10']:.1f}% · top 20 {hd['top20']:.1f}% (pools excluded; in pools {hd['pooled_pct']:.1f}%, burned {hd['burned_pct']:.1f}%)")
        if hd.get("top10_chg") is not None:
            m2.append(f"Top-10 share {hd['top10_chg']:+.1f} pts over {hd['trend_window']}: "
                      + ("concentrating" if hd["top10_chg"] > 1 else "distributing" if hd["top10_chg"] < -1 else "stable"))
        m2.append("Largest: " + ", ".join(f"{x['pct']:.1f}%{'*' if x['insider'] else ''}" for x in hd["top"][:5])
                  + (" (* = insider)" if any(x["insider"] for x in hd["top"][:5]) else ""))
        m2.append(f"Deployer {e(short(hd['creator']))}: " + (f"{hd['dev_pct']:.2f}%" if hd["dev_pct"] is not None else "unknown"))
        m2.append(f"Insiders {hd['insider_pct']:.1f}% · insider wallets in graph: {hd['graph_insiders']}")
    else:
        m2.append("Unavailable: RugCheck report could not be fetched")
    m2 += ["<b>D. Social</b>", social_line(mk),
           "<b>E. Security</b>", f"<b>{a['risk_band']}</b>" + (f" · risk {a['risk']}/100" if a["risk"] is not None else ""),
           fmt_findings(a["findings"], ("critical", "high", "medium", "low", "ok", "unknown")),
           "<b>F. Bubble Maps / Thesis</b>"]
    if hd and hd["networks"]:
        m2.append("Linked-wallet clusters (RugCheck insider graph, used instead of Bubblemaps):\n" + "\n".join(
            f"• {n['size']} wallets linked by {e(n['type'])}, hold {pct(n['pct'], False)} ({n['active']} active)"
            for n in hd["networks"][:5]))
    elif hd:
        m2.append("No linked-wallet clusters detected by RugCheck's graph.")
    m2.append(e(P.UNAVAILABLE["bubblemaps"]) + ". " + e(P.UNAVAILABLE["thesis"]) + ".")
    reason = "yes: " + a["verdict"].lower() if a["verdict"] in VERDICTS[:2] else "not now: " + a["verdict"].lower()
    m3 = ["<b>Verdict</b>",
          f"{verdict_icon(a['verdict'])} <b>{a['verdict']}</b>",
          f"Intelligence score <b>{a['score']}/100</b> (scored on {a['coverage']:.0%} of weights) · "
          f"risk {a['risk'] if a['risk'] is not None else 'n/a'}/100 ({a['risk_band']})",
          "Category scores: " + ", ".join(f"{CAT_NAMES[k]} {v:.0f}" if v is not None else f"{CAT_NAMES[k]} n/a"
                                          for k, v in a["cats"].items()),
          "Adjustments: " + (", ".join(f"{d:+d} {e(w)}" for d, w in a["adjust"]) or "none"),
          "➕ " + ("\n➕ ".join(e(x) for x in a["positives"][:6]) or "none"),
          "➖ " + ("\n➖ ".join(e(x) for x in a["negatives"][:8]) or "none"),
          "Missing: " + "; ".join(e(x) for x in a["missing"]),
          f"📈 Bull case: {e(a['scenarios']['bull'])}",
          f"📉 Bear case: {e(a['scenarios']['bear'])}",
          f"Worth investigating further? {reason}.",
          "Why: " + why_text(a),
          "<i>A high score means the evidence looks better than most, not that the price will rise.</i>",
          src_line(a)]
    return ["\n".join(m1), "\n".join(m2), "\n".join(m3)]


def why_text(a):
    if a["disqualified"]:
        crit = [f["text"] for f in a["findings"] if f["sev"] == "critical"]
        return esc(crit[0] if crit else f"risk score {a['risk']} is at or above 60")
    if a["verdict"] == VERDICTS[2]:
        if a["coverage"] < 0.6 or not a["holders"]:
            return f"too much data missing (scored on {a['coverage']:.0%} of the weights)"
        return f"score {a['score']} is below 55: mixed or weak evidence"
    return esc(f"score {a['score']} with risk {a['risk']}; strongest points: " + "; ".join(a["positives"][:2]))


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else (addr or "?")


def fmt_rugcheck(a):
    mk = a["market"]
    e = esc
    name = f"${e(mk['symbol'])}" if mk else e(short(a["mint"]))
    verified = [f for f in a["findings"] if f["sev"] == "ok" and f["status"] == "verified"]
    noissue = [f for f in a["findings"] if f["sev"] == "ok" and f["status"] != "verified"]
    warns = [f for f in a["findings"] if f["sev"] in ("critical", "high", "medium", "low")]
    unknown = [f for f in a["findings"] if f["sev"] == "unknown"]
    L = [f"🛡 <b>Rug check {name}</b>", f"<code>{e(a['mint'])}</code>",
         f"Risk <b>{a['risk'] if a['risk'] is not None else 'n/a'}/100</b>: <b>{a['risk_band']}</b>"]
    if mk:
        L.append(f"Liquidity {usd(mk['liq'])}" + (f" ({pct(mk['liq_chg_1h'])} vs 1h ago)" if mk.get("liq_chg_1h") is not None else "")
                 + f" · MC {usd(mk['mcap'])} · {e(mk['graduation'])}")
    hd = a["holders"]
    if hd:
        L.append(f"Top 10 {hd['top10']:.1f}% · top 20 {hd['top20']:.1f}% · deployer "
                 + (f"{hd['dev_pct']:.2f}%" if hd["dev_pct"] is not None else "?") + f" · insiders {hd['insider_pct']:.1f}%")
    L.append(f"\n<b>Warnings ({len(warns)})</b>\n" + (fmt_findings(warns, ("critical", "high", "medium", "low")) or "none detected"))
    L.append("\n<b>Verified safe properties</b> (checked on-chain)\n" + (fmt_findings(verified, ("ok",)) or "none verified"))
    L.append("\n<b>Checked, no issue found</b> (not proof of safety)\n" + (fmt_findings(noissue, ("ok",)) or "none"))
    L.append("\n<b>Unknown</b>\n" + "\n".join(["❔ " + e(f["text"]) for f in unknown] +
                                              ["❔ " + e(x) for x in a["missing"]]))
    used = [k for k, v in a["src"].items() if v["ok"]]
    L.append("\nSources used: " + (", ".join(used) or "none"))
    L.append("<i>Scores are heuristics. No warnings does not mean safe.</i>")
    L.append(src_line(a))
    text = "\n".join(L)
    if len(text) <= 4000:
        return [text]
    cut = text.rfind("\n", 0, 3900)
    return [text[:cut], text[cut + 1:]]


def snapshot_row(a):
    mk, hd = a["market"] or {}, a["holders"] or {}
    return dict(t=a["t"], score=a["score"], risk=a["risk"], verdict=a["verdict"], price=mk.get("price"),
                cats={k: (round(v) if v is not None else None) for k, v in a["cats"].items()},
                sym=mk.get("symbol"), mcap=mk.get("mcap"), liq=mk.get("liq"), top10=hd.get("top10"))
