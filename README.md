# Edge Finder

A 24/7 engine that looks for +EV prices on **Kalshi** and **Polymarket US** sports markets by comparing them with a no-vig "fair" price blended from sharp sportsbooks and exchanges (Pinnacle, Betfair Exchange, BetOnline, Novig, ProphetX). It paper-trades the edges it finds, tracks closing line value (CLV), grades results, and serves a local dashboard.

**Status:** paper trading only. No real orders are placed. Nothing here is financial or betting advice; use at your own risk and follow the terms of every service you connect.

## How it works

1. **Sharp prices**: [The Odds API](https://the-odds-api.com) for 10 books per request (1 credit per market per sport), budgeted to last the month.
2. **Fair price**: power-method devig of each sharp book, blended by weight. Bars drop when 3+ sharp books agree.
3. **Live venue prices**: Kalshi and Polymarket US WebSocket streams (moneylines, spreads, totals), with REST polling as a fallback.
4. **Edge check**: EV after each venue's taker fee (both use `fee = θ · C · p · (1 − p)`). A live edge with stale sharp prices triggers a 1-credit re-check of that single game.
5. **Paper bets**: quarter Kelly, capped at 2% per bet, one bet per game, max 25% of bankroll at risk, capped at the size actually for sale.
6. **Learning data**: every comparison is logged; a tracking list records every positive-EV price as a flat pretend bet to measure which EV levels beat the close.
7. **News**: ESPN injury feeds (NBA, NFL, NHL) trigger immediate re-checks and hold bets while sharp prices are older than the news.

## Setup (macOS)

```bash
cp .env.example .env && chmod 600 .env     # fill in your keys
python3 engine.py --once --no-text         # one test cycle
./install_engine.sh                        # run 24/7 as a LaunchAgent
open http://localhost:8787                 # dashboard
```

Requires Python 3.11+ (standard library only) and OpenSSL 3 (`brew install openssl`) for Ed25519 request signing. iMessage alerts use the macOS Messages app.

## Files

| File | What |
|---|---|
| `engine.py` | Main loop: budget, fetch, bet, grade, recap, website |
| `edge_finder.py` | Devig, sharp consensus, fees, Kelly, scanning |
| `live.py` | Kalshi + Polymarket US streams and market matching |
| `wsclient.py` | Minimal WebSocket client and request signing |
| `news.py` | ESPN injury watcher |
| `webserver.py`, `ui.py`, `dashboard.py`, `tracking_page.py`, `monitor.py` | Local website |
| `config.json` | Sports, bars, weights, bankroll, fees |
| `PLAN.md` | Roadmap |
