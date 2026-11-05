# Edge Finder: Plan

Goal: a system that runs 24/7, finds bets on Kalshi and Polymarket US that are priced better than the sharp market says they should be, bets them automatically within strict limits, and gets smarter from its own results.

Started: Nov 4, 2026. Current mode: **paper trading** ($500 Kalshi + $500 Polymarket US, simulated).

---

## How it works today

```
FREE                                   PAID (The Odds API, 20K credits/mo)
ESPN injury feeds (every 3 min) ──┐    Odds for 10 books per request
Game schedules (every 10 min) ────┼──► (1 credit per market per sport)
                                  │            │
                                  ▼            ▼
                     [ engine.py on the Mac, 24/7 ]
                       1. Blend 5 sharp books into a fair price
                       2. Compare Kalshi / Poly US prices after fees
                       3. Bet (paper) when EV clears the bar
                       4. Track closing line, grade results
                                  │
              ┌───────────────────┼──────────────────┐
              ▼                   ▼                  ▼
        iMessage alerts     Local website       logs/ (training data)
        + 9am recap         localhost:8787
```

### Fair price (who we trust)
| Book | Weight | Role |
|---|---|---|
| Pinnacle | 50% | Sharpest book, the anchor |
| Betfair Exchange | 20% | Largest exchange, pro-set prices |
| BetOnline | 10% | Sharp-leaning book |
| Novig | 10% | US exchange |
| ProphetX | 10% | US exchange |

Display only: DraftKings, FanDuel, Caesars. Bet venues: Kalshi, Polymarket US.

### Betting rules
- **EV bar:** 1.5% (moneyline) / 2% (spread, total) when 3+ sharp books agree within 2 points. 3% otherwise. +1% if the sharp market is wide.
- **Skip:** EV over 15% (almost always stale data or a rules mismatch), games under 20 min or over 36 h away, prices older than 15 min.
- **News guard:** if a team has injury news newer than Pinnacle's last update, wait.
- **Sizing:** quarter Kelly, capped at 2% of that venue's bankroll per bet. Sizes off the current balance, so wins compound.
- **Exposure:** one bet per game; never more than 25% of the bankroll riding at once.
- **Ramp:** stay at quarter Kelly until 200+ bets show positive CLV, then consider 0.35 to 0.5 Kelly. Never full Kelly.
- **Fees:** Kalshi 0.07 x P x (1-P) per contract. Polymarket US 0.0695 x P x (1-P) per contract (verified from docs.polymarket.us/fees, Nov 4 2026; replaces the old 1% guess).

### Credit budget
- Each full check costs about 3 credits per sport (moneyline + spread + total, 10 books).
- Engine paces itself to last until month end and keeps 1,000 in reserve.
- Sports with a game in the next 6 hours are checked most often (about every 25 min now).
- Injury news can trigger an extra check (max 40 per day).

---

## Roadmap

### Phase 1: Prove the edge (now through ~Nov 18)  ✅ running
- [x] Engine, paper trading, iMessage alerts, daily recap
- [x] Dashboard + OddsJam-style odds screen
- [x] Fair price blended from 5 sharp books
- [x] ESPN injury watcher with news-triggered checks and stale-line guard
- [x] Every price comparison logged to `logs/observations.csv` (ML training data)
- [x] Polymarket US fee schedule verified (0.0695 x P x (1-P))
- [ ] Confirm Kalshi fee with one real $1 trade
- [x] Local monitoring website (http://localhost:8787)

**Go-live test:** after 2 weeks or 50+ graded paper bets, average closing line value (CLV) is positive and at least 55% of bets beat the closing line. Win/loss record alone is not the test; it is mostly luck at this sample size.

### Phase 2: Real-time prices  ✅ done Nov 4
- [x] Live Kalshi (every 10 s) and Polymarket US (every 30 s) moneyline prices, free, matched to every game; they replace the Odds API's delayed copy (live.py)
- [x] Paper bets capped at the size actually for sale at the best Kalshi price
- [x] Real-time WebSocket streams: Kalshi ticker channel and Polymarket US full order book, with polling as automatic fallback if a stream drops (wsclient.py, no extra packages)
- [x] Polymarket US size check: paper bets capped at the contracts actually offered at the best price
- [x] Live prices for spreads and totals (Kalshi spread/total series; Polymarket US full-game spreads/totals), matched to the exact sharp line
- [x] When a live price shows an edge but sharp prices are over 3 min old, re-check that one game for 1 credit before betting (max 60/day)

### Soccer focus (added Nov 4)
- [x] 14 soccer leagues: EPL, MLS, La Liga, Bundesliga, Bundesliga 2, Serie A, Ligue 1, Champions League, Europa League, Conference League, Championship, Liga MX, Eredivisie, Primeira Liga
- [x] Kalshi soccer moneylines (3-way incl. Tie), spreads and totals matched, with fuzzy club-name matching (PSG, Atletico, Bilbao, Stade Rennais)
- [x] Paper tracking list (/tracking): every positive-EV live price as a $10 flat pretend bet, graded with CLV, bucketed by EV level
- [ ] Polymarket US soccer moneylines (3-way markets need a different matcher)
- [ ] After ~1 week: keep the leagues that beat the close, drop the rest

### Phase 3: Go live with small limits
- [ ] Move the engine to a small US cloud server ($5 to $12/mo) so it runs regardless of the Mac; alerts via Telegram or relayed to the Mac for iMessage
- [ ] Live order placement on Kalshi and Polymarket US (keys already saved and tested)
- [ ] Hard limits: $5 max per bet, $15 max loss per day, limit orders only at or under the alert price, one-command kill switch
- [ ] Raise limits only while CLV stays positive

### Phase 4: Learning system  (core loop live Nov 5)
- [x] **learner.py**: every 6 h, fits CLV = a + b x EV per segment (venue x sport x market x fair source), shrunk toward a prior (60% edge retention, worth 25 bets). Sets each segment's EV bar so expected CLV >= 1%, blocks segments where retention < 15%. Needs 30 graded picks per segment before it overrides the default bars. Only paper bets are gated; the tracking list keeps sampling everything so blocked segments can earn their way back. Report: `python3 learner.py --dry`. Rules: logs/policy.json. Summary line in the 9am recap.
- [x] **soccer_model.py**: Dixon-Coles score model fitted to the sharp no-vig 1X2 + .5 totals/handicaps (held-out error 0.18 pts on 158 Pinnacle lines). Prices Kalshi soccer lines Pinnacle doesn't hang (e.g. "wins by 2+"). Those rows are tracking-only until the learner sees 30 graded picks with real retention.
- [x] **combos.py**: prices every 2-leg same-game soccer combo (result, total, 2+ margin, BTTS) for leagues Kalshi allows in combos; writes logs/combos.json with "buy below" price targets.
- [ ] Kalshi combo quotes: combos are priced on request (RFQ to market makers). Needs an RFQ client; off until approved.
- [ ] Label every logged price with its closing line (no need to wait for game results)
- [ ] Train a gradient-boosted tree model (LightGBM) to predict "will this price beat the close?" using: sport, market, venue, EV, sharp book count and agreement, time to start, minutes since news, price movement
- [ ] Learn sharp book weights from data instead of fixed guesses (same idea as OddsJam's "Recommended Book Weights")
- [ ] Walk-forward testing only: train on past days, test on later unseen days
- [ ] Per-segment bars: lower where the model shows we win, raise or turn off where we do not
- [ ] Deep learning only once there are hundreds of thousands of rows and a clear reason

### Phase 5: More signal
- [ ] More news sources: NBA official injury report and lineups, NHL starting goalies, NFL inactives and weather, soccer lineups
- [ ] Polymarket top-wallet tracking as a second signal
- [ ] Player props (credit-heavy; after the 100K credit plan, $59/mo)
- [ ] Upgrade Odds API plan if the engine runs short on credits

---

## Key numbers to watch
| Metric | Where | Good |
|---|---|---|
| Avg CLV | Dashboard, recap | Positive over 50+ bets |
| Beat closing line % | Dashboard, recap | 55%+ |
| Credits left | Dashboard | Lasting to month end |
| Bets per day | Dashboard | A few; zero most hours is normal |

## Risks
- Fee assumptions wrong (Polymarket US especially) can turn small edges negative.
- Kalshi / Polymarket rules can differ from sportsbooks (overtime, postponements); a "too good" edge is usually this.
- Sharp (Pinnacle etc.) prices are still polled, every ~25 min plus news and live-edge re-checks; venue prices are now real time. More credits = fresher fair prices.
- Mac must stay on and plugged in; lid closed = sleep.
- API keys were pasted in chat: rotate them.

## Files
| File | What |
|---|---|
| `engine.py` | 24/7 loop: budget, fetch, bet, grade, recap, website |
| `edge_finder.py` | Math: devig, sharp consensus, fees, Kelly, scanning |
| `news.py` | ESPN injury watcher |
| `dashboard.py` | Builds dashboard.html and odds.html |
| `config.json` | All settings (bars, weights, bankroll, sports) |
| `install_engine.sh` | Start / stop the 24/7 service |
| `logs/paper_state.json` | Bets, balances, engine state |
| `logs/observations.csv` | Every price comparison (ML data) |
| `logs/news.csv` | Every injury status change |
| `logs/engine.log` | What the engine is doing |
