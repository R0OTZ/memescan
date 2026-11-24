# memescan

A free Solana memecoin scanner that sends alerts to Telegram. You place the trades yourself on fomo.

Every ~10 minutes (via GitHub Actions) it:
1. Pulls trending Solana tokens (DexScreener boosts and new profiles, GeckoTerminal trending).
2. Drops dead, thin and too-new tokens, then rug-checks the rest with RugCheck: mint/freeze authority, danger-level risks, insider wallet clusters, creator history.
3. Sends one BUY card at a time (with an exit plan), WAIT cards with a dip target, and logs AVOIDs.
4. Watches your wallet and sends SELL NOW (rug or dev selling), STOP-LOSS (-18%), TAKE PROFIT (2x) and TRAILING STOP alerts.
5. Grades its own calls. Telegram commands: /scan, /positions, /stats (replies arrive on the next run).

Secrets (Settings > Secrets and variables > Actions): TELEGRAM_BOT_TOKEN, WALLET.
Thresholds live in CFG at the top of scanner.py. Paper P&L in /stats ignores fees and slippage.
