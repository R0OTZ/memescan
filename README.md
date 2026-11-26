# memescan

A free Solana memecoin intelligence bot on Telegram, built around fomo. It researches, rug-checks, watches and
alerts. **It never trades for you**: copy trading is simulated (paper fills at real Jupiter quotes).

## What runs (GitHub Actions, $0, one hour at a time, each hour starts the next)
| Process | Does | How often |
|---|---|---|
| `listener.py` | answers Telegram commands | instantly |
| `watcher.py` | `/watch` tokens · copy-watch/copy-trade wallets · `/best` discovery | 10 s · 15 s · 2 min |
| `scanner.py` | your holdings (stop/take-profit/rug), `/summary`, `/stats`, dips, digest | ~every minute |

## Commands
- `/best`: the strongest current opportunities. It draws on fomo trending and gainers, fresh PumpSwap graduations, GeckoTerminal trending, DexScreener boosts and new profiles, volume spikes and holder growth. Coins with no trades in the last 5 minutes are dropped. The top 10 get the full analysis and are scored, and `/best` re-checks prices live when you ask. If nothing qualifies, it says so.
- `/token <CA>`: a full research report covering overview, market behavior, holders, social, security and clusters, with a verdict and its reasons.
- `/rugcheck <CA>`: a 0–100 risk score with every warning and its evidence. It separates verified properties, checks that found nothing, and unknowns.
- `/watch <CA>`, `/unwatch`, `/watchlist`, `/watchstatus`, `/watchpause`, `/watchresume`: checks every 10 s and alerts only on meaningful changes.
- `/notifications on|off|status|settings`: background alerts only. Commands never trigger them.
- `/copywatch [add <name> <address> | remove | stats <name> | activity <name>]`: alerts only.
- `/copytrade [set|change|wallet|remove|on|off|pause|resume|status|settings|stats]`: an ON/OFF panel with simulated execution.
- `/summary`, `/stats`, `/positions`, `/calls`, `/help`

## Data sources (all free)
DexScreener (market), RugCheck (holders, insiders, linked-wallet clusters, LP), Solana RPC (mint/freeze authority,
Token-2022 extensions, wallet history), Jupiter (sell-route check, simulated fills), GeckoTerminal (trending, last 300
trades with wallets), fomo public feed.
**Not available at $0**, so the bot reports them as missing and never fakes them:
- X/Twitter mentions and sentiment (the X API is pay-per-use since Feb 2026)
- the Bubblemaps API (needs a paid key; RugCheck's insider graph is used instead)
- "Thesis" (no public API found)

Optional: add a free Helius key as the `HELIUS_API_KEY` secret for steadier Solana RPC.

## Scoring
The `/best` and `/token` score is weighted by category:
- momentum 20
- liquidity/activity 15
- holders 15
- wallet intelligence 15
- X/social 15
- safety 15
- lifecycle 5

Missing categories are excluded and the report shows the share of weights actually scored.

Penalties apply for stale data, no recent trades, fading volume, late entries, wash-like volume and thin liquidity. A critical security finding, or a risk score of 60 or more, disqualifies a token whatever its score.

Weights are configurable with a `state/weights.json` file or the `MEMESCAN_WEIGHTS` env setting. `/calls` reports whether high-scoring categories actually did better once there are 20 resolved picks.

## Secrets
`TELEGRAM_BOT_TOKEN`, `WALLET`, optional `TELEGRAM_CHAT_ID`, optional `HELIUS_API_KEY`. No private key, ever.

## Tests
`for t in tests/test_*.py; do python3 $t; done`. They run fully offline on saved real API responses.
