"""Shared data providers for memescan.

Every fetch returns a Res dict: {ok, data, source, fetched, error, cached}. Callers never get old data
silently: cached results carry their real fetch time, and freshness() turns an age into a label.

Free sources used (no keys needed):
  DexScreener   market data, pairs, boosts           ~300 req/min on /tokens
  RugCheck      holders, insiders, clusters, LP, risks
  Solana RPC    mint account (authorities, Token-2022 extensions), wallet history
  GeckoTerminal trending/new pools, last 300 trades with wallets   (few calls/min)
  Jupiter       sell-route quote (is there a way to sell this token?)
  fomo          public price feed behind fomo.family's market pages
Optional, only if you add a key as a GitHub secret:
  HELIUS_API_KEY  faster, higher-limit Solana RPC (free plan: 1M credits/month)
Not available on a $0 budget (reported as missing, never faked): X/Twitter search (paid since Feb 2026),
Bubblemaps API (paid Pro key), "Thesis" (no public API found).
"""
import calendar
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RC = "https://api.rugcheck.xyz/v1"
JUP = "https://lite-api.jup.ag/swap/v1/quote"
FOMO = "https://prod-api.fomo.family/public/prices"
FOMO_SOLANA = 1399811149
WSOL = "So11111111111111111111111111111111111111112"
UA = {"User-Agent": "Mozilla/5.0 (memescan)", "Accept": "application/json"}

UNAVAILABLE = {
    "x": "X/Twitter: no free read access (X API is pay-per-use since Feb 2026)",
    "bubblemaps": "Bubblemaps: API needs a paid Pro key; RugCheck's insider-network graph is used instead",
    "thesis": "Thesis: no public holder-intelligence API found",
}

# minimum seconds between calls to one host, per process (free APIs ban bursts)
MIN_GAP = {"api.geckoterminal.com": 2.2, "api.rugcheck.xyz": 0.35, "api.dexscreener.com": 0.2,
           "lite-api.jup.ag": 0.5, "api.mainnet-beta.solana.com": 0.15, "solana-rpc.publicnode.com": 0.1}
_last = {}
_lock = threading.Lock()
STATS = {"calls": 0, "errors": {}, "rate_limited": {}}


def _host(url):
    return urllib.parse.urlparse(url).netloc


def _pace(host):
    gap = MIN_GAP.get(host, 0.0)
    if not gap:
        return
    with _lock:
        wait = _last.get(host, 0) + gap - time.time()
        _last[host] = max(time.time(), _last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


def res(ok, data=None, source="", error=None, fetched=None, cached=False):
    return dict(ok=ok, data=data, source=source, error=error, fetched=fetched or time.time(), cached=cached)


def http_json(url, method="GET", body=None, timeout=12, tries=3, source=""):
    """GET/POST JSON with pacing, retries and exponential backoff with jitter. Honors Retry-After."""
    host = _host(url)
    data = json.dumps(body).encode() if body is not None else None
    hdr = dict(UA)
    if data:
        hdr["Content-Type"] = "application/json"
    err = None
    for i in range(tries):
        _pace(host)
        STATS["calls"] += 1
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return res(True, json.loads(r.read().decode()), source or host)
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            try:
                body_err = json.loads(e.read().decode())
            except Exception:
                body_err = None
            if e.code in (400, 404) and isinstance(body_err, dict):
                return res(False, body_err, source or host, error=err)
            if e.code == 429:
                STATS["rate_limited"][host] = STATS["rate_limited"].get(host, 0) + 1
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                delay = float(ra) if ra and ra.isdigit() else (1.5 * 2 ** i)
                time.sleep(min(delay, 10) + random.random() * 0.5)
                continue
            break
        except Exception as e:  # timeout, DNS, bad JSON
            err = type(e).__name__
            if i < tries - 1:
                time.sleep(1.0 * 2 ** i + random.random() * 0.3)
                continue
    STATS["errors"][host] = STATS["errors"].get(host, 0) + 1
    return res(False, None, source or host, error=err)


class Cache:
    """TTL cache, optionally saved to disk so short-lived processes (one scan a minute) share it.
    get() returns the stored Res with its original fetch time, so freshness is never faked."""

    def __init__(self, path=None, max_items=2000):
        self.path, self.max = path, max_items
        self.d = {}
        if path:
            try:
                with open(path) as f:
                    self.d = json.load(f)
            except Exception:
                self.d = {}

    def get(self, key, ttl):
        r = self.d.get(key)
        if r and time.time() - r["fetched"] < ttl:
            return dict(r, cached=True)
        return None

    def last(self, key):
        return self.d.get(key)

    def put(self, key, r):
        if r.get("ok"):
            self.d[key] = r
        return r

    def save(self):
        if not self.path:
            return
        now = time.time()
        items = sorted(((k, v) for k, v in self.d.items() if now - v["fetched"] < 6 * 3600),
                       key=lambda kv: -kv[1]["fetched"])[:self.max]
        self.d = dict(items)
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.d, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass


MEM = Cache()  # default in-memory cache

# how long each kind of data may be reused (seconds)
TTL = dict(market=15, trades=120, holders=300, security=600, mint=1800, meta=6 * 3600, discovery=120,
           route=300)


def cached(cache, key, ttl, fn):
    if cache is False:  # caller wants a live fetch (the 10-second watch feed)
        return fn()
    cache = cache or MEM
    hit = cache.get(key, ttl)
    if hit:
        return hit
    return cache.put(key, fn())


def freshness(age):
    if age is None:
        return "unknown"
    if age <= 30:
        return "live"
    if age <= 120:
        return "fresh"
    if age <= 600:
        return "recent"
    return "stale"


def age_of(r, now=None):
    return None if not r or not r.get("fetched") else max(0.0, (now or time.time()) - r["fetched"])


# ---------------------------------------------------------------- DexScreener
def ds_tokens(mints, cache=None):
    """{mint: [pairs]} for up to any number of mints (batched 30 per call). Returns Res with data=dict."""
    mints = [m for m in dict.fromkeys(mints) if m]
    out, ok, err, fetched = {}, True, None, time.time()
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        key = "ds:" + ",".join(chunk)
        r = cached(cache, key, TTL["market"],
                   lambda: http_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}", source="DexScreener"))
        if not r["ok"]:
            ok, err = False, r["error"]
            continue
        fetched = min(fetched, r["fetched"])
        for p in r["data"] if isinstance(r["data"], list) else []:
            m = (p.get("baseToken") or {}).get("address")
            if m in chunk:
                out.setdefault(m, []).append(p)
    return res(ok or bool(out), out, "DexScreener", err, fetched)


def best_pair(pairs):
    return max(pairs or [], key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), default=None)


def ds_list(path, cache=None):
    return cached(cache, "dsl:" + path, TTL["discovery"], lambda: http_json(DS + path, source="DexScreener"))


# ---------------------------------------------------------------- RugCheck
def rugcheck(mint, cache=None, ttl=None):
    return cached(cache, "rc:" + mint, ttl or TTL["holders"],
                  lambda: http_json(f"{RC}/tokens/{mint}/report", source="RugCheck", tries=2))


# ---------------------------------------------------------------- Solana RPC
def rpc_urls():
    urls = []
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if key:
        urls.append(f"https://mainnet.helius-rpc.com/?api-key={key}")
    urls.append(os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com"))
    urls.append("https://solana-rpc.publicnode.com")
    return list(dict.fromkeys(urls))


_rpc_fail = {}


def rpc(method, params, timeout=10):
    """JSON-RPC with fallback across endpoints; an endpoint failing 3 times in a row is rested 60 s."""
    last_err = None
    for url in rpc_urls():
        host = _host(url)
        f = _rpc_fail.get(host, [0, 0])
        if f[0] >= 3 and time.time() - f[1] < 60:
            continue
        r = http_json(url, "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=timeout, tries=1, source="Solana RPC")
        d = r["data"] if r["ok"] else None
        if isinstance(d, dict) and "result" in d:
            _rpc_fail[host] = [0, 0]
            return res(True, d["result"], "Solana RPC")
        last_err = (d or {}).get("error", {}).get("message") if isinstance(d, dict) else r["error"]
        _rpc_fail[host] = [f[0] + 1, time.time()]
    return res(False, None, "Solana RPC", error=str(last_err)[:80])


def mint_account(mint, cache=None):
    """On-chain mint state: authorities, supply, program and Token-2022 extensions (verified, not reported)."""
    def fetch():
        r = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not r["ok"]:
            return r
        v = (r["data"] or {}).get("value")
        if not v:
            return res(False, None, "Solana RPC", error="account not found")
        parsed = ((v.get("data") or {}).get("parsed") or {})
        info = parsed.get("info") or {}
        if parsed.get("type") != "mint":
            return res(False, None, "Solana RPC", error="not a token mint")
        ext = {e.get("extension"): e.get("state") for e in info.get("extensions") or []}
        return res(True, dict(program=v.get("owner"), mint_authority=info.get("mintAuthority"),
                              freeze_authority=info.get("freezeAuthority"), supply=int(info.get("supply") or 0),
                              decimals=int(info.get("decimals") or 0), extensions=ext), "Solana RPC")
    return cached(cache, "mint:" + mint, TTL["mint"], fetch)


def address_kind(addr):
    """What an address is on-chain: 'wallet', 'token_mint', 'token_account', 'program_owned', 'unused' or None (RPC
    failed). A fomo wallet often holds no SOL, so an address with no account but with transactions is a wallet."""
    r = rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    if not r["ok"]:
        return None
    v = (r["data"] or {}).get("value")
    if v is None:
        s = rpc("getSignaturesForAddress", [addr, {"limit": 1}])
        if not s["ok"]:
            return None
        return "wallet" if s["data"] else "unused"
    if v.get("owner") == "11111111111111111111111111111111":
        return "wallet"
    t = (((v.get("data") or {}) if isinstance(v.get("data"), dict) else {}).get("parsed") or {}).get("type")
    return {"mint": "token_mint", "account": "token_account"}.get(t, "program_owned")


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def valid_address(s):
    """A Solana address is base58 that decodes to 32 bytes."""
    if not isinstance(s, str) or not 32 <= len(s) <= 44 or any(c not in B58 for c in s):
        return False
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    lead = len(s) - len(s.lstrip("1"))
    return lead + (n.bit_length() + 7) // 8 == 32


# ---------------------------------------------------------------- GeckoTerminal
def gt(path, cache=None, ttl=None):
    return cached(cache, "gt:" + path, ttl or TTL["discovery"], lambda: http_json(GT + path, tries=1,
                                                                                    source="GeckoTerminal"))


def gt_trades(pool, cache=None):
    """Last up-to-300 trades on a pool (24h window) with wallet, side, USD and time."""
    key = f"/networks/solana/pools/{pool}/trades"
    r = cached(cache, "gt:" + key, TTL["trades"], lambda: http_json(GT + key, tries=2, source="GeckoTerminal"))
    if not r["ok"]:
        return r
    rows = []
    for t in (r["data"] or {}).get("data") or []:
        a = t.get("attributes") or {}
        try:
            ts = calendar.timegm(time.strptime(a["block_timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
            rows.append(dict(ts=ts, kind=a.get("kind"), usd=float(a.get("volume_in_usd") or 0),
                             wallet=a.get("tx_from_address"), anomalous=bool(a.get("is_anomalous"))))
        except (KeyError, ValueError, TypeError):
            continue
    return dict(r, data=rows)


# ---------------------------------------------------------------- Jupiter
def sell_route(mint, decimals, cache=None):
    """Does Jupiter find a route to sell ~1,000 tokens for SOL? Evidence the token is sellable, not proof."""
    amt = 1000 * 10 ** decimals

    def fetch():
        r = http_json(f"{JUP}?inputMint={mint}&outputMint={WSOL}&amount={amt}&slippageBps=1000", tries=2,
                      source="Jupiter")
        d = r["data"] if isinstance(r.get("data"), dict) else {}
        code = d.get("errorCode")
        if code in ("TOKEN_NOT_TRADABLE", "COULD_NOT_FIND_ANY_ROUTE", "NO_ROUTES_FOUND"):
            return res(True, dict(route=False, code=code), "Jupiter")  # Jupiter's own "can't sell this" answer
        if not r["ok"]:
            return res(False, None, "Jupiter", error=r["error"])
        return res(True, dict(route=bool(d.get("routePlan")), impact=float(d.get("priceImpactPct") or 0),
                              dexes=[(x.get("swapInfo") or {}).get("label") for x in d.get("routePlan") or []]),
                   "Jupiter")
    return cached(cache, "jup:" + mint, TTL["route"], fetch)


# ---------------------------------------------------------------- fomo
def fomo_feed(cache=None):
    """Solana tokens on fomo's public market feed, parsed: {mint: dict(...)}."""
    def fetch():
        r = http_json(FOMO, source="fomo", timeout=20)
        if not r["ok"]:
            return r
        out = {}
        for t in ((r["data"] or {}).get("responseObject") or {}).get("tokens") or []:
            al, md = t.get("allowlist") or {}, t.get("marketData") or {}
            if al.get("networkId") != FOMO_SOLANA or not al.get("tokenAddress"):
                continue

            def num(k):
                try:
                    return float(md.get(k) or 0)
                except (TypeError, ValueError):
                    return 0.0
            out[al["tokenAddress"]] = dict(
                ticker=al.get("ticker"), vol1=num("volume1"), vol5=num("volume5m"), ch1=num("change1") * 100,
                ch5=num("change5m") * 100, ch4=num("change4") * 100, ch24=num("change24") * 100,
                txn1=num("txnCount1"), holders=md.get("holders"), mcap=num("marketCap"), liq=num("liquidity"),
                created=md.get("createdAt"),
                x=(((md.get("token") or {}).get("socialLinks") or {}).get("twitter")))
        return res(True, out, "fomo")
    return cached(cache, "fomo", 30, fetch)
