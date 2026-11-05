#!/usr/bin/env python3
"""Edge Finder: daily +EV bets on Kalshi and Polymarket, priced against Pinnacle.

How it works:
  1. Pull odds for Pinnacle, Kalshi and Polymarket from The Odds API.
  2. Remove Pinnacle's margin (power devig) to get a fair probability per outcome.
  3. Compare each Kalshi/Polymarket price to fair, after fees.
  4. Keep bets that clear the EV bar for their market type, size them with
     fractional Kelly, log them, and text the best ones via iMessage.

Usage:
  python3 edge_finder.py --dry-run   # print the message, don't text
  python3 edge_finder.py             # run and text
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "logs" / "flags.csv"
SENT_PATH = HERE / "logs" / "sent.json"
ENV_PATH = Path.home() / ".odds_api_env"
API = "https://api.the-odds-api.com/v4"

VENUE_LABEL = {"kalshi": "Kalshi", "polymarket": "Polymarket", "polymarket_us": "Polymarket US"}
MARKET_LABEL = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}


# ---------- setup ----------

def load_api_key():
    import secrets_env
    key = secrets_env.get("ODDS_API_KEY")
    if key:
        return key
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("ODDS_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"No API key. Put ODDS_API_KEY=... in {ENV_PATH}")


def fetch_odds(key, sport, cfg):
    books = book_list(cfg)
    params = {
        "apiKey": key,
        "bookmakers": ",".join(books),
        "markets": ",".join(cfg["markets"]),
        "oddsFormat": "decimal",
    }
    url = f"{API}/sports/{sport}/odds?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            remaining = r.headers.get("x-requests-remaining")
            return json.load(r), remaining
    except urllib.error.HTTPError as e:
        # Some markets (e.g. spreads for MMA) are invalid for some sports: retry h2h only.
        if e.code == 422 and cfg["markets"] != ["h2h"]:
            return fetch_odds(key, sport, {**cfg, "markets": ["h2h"]})
        print(f"[{sport}] HTTP {e.code}: {e.read()[:200]!r}", file=sys.stderr)
        return [], None


# ---------- math ----------

def power_devig(decimal_odds):
    """Fair probabilities via the power method: find k with sum(p_i^k) = 1."""
    implied = [1 / d for d in decimal_odds]
    lo, hi = 1.0, 10.0
    for _ in range(100):
        k = (lo + hi) / 2
        s = sum(p ** k for p in implied)
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    fair = [p ** k for p in implied]
    total = sum(fair)
    return [p / total for p in fair]


def effective_cost(venue, decimal_price, cfg):
    """Cost per $1 payout after fees (an exchange 'price' in dollars)."""
    p = 1 / decimal_price
    if venue == "kalshi":
        # Kalshi taker fee per contract ~ coefficient * P * (1 - P). Assumption: verify.
        return p + cfg["fees"]["kalshi_taker_coefficient"] * p * (1 - p)
    if venue.startswith("polymarket"):
        # Polymarket US taker fee = 0.0695 * C * p * (1 - p)  (docs.polymarket.us/fees, Oct 28 2026)
        return p + cfg["fees"]["polymarket_us_taker_coefficient"] * p * (1 - p)
    return p


def to_american(decimal_odds):
    if decimal_odds >= 2:
        return f"+{round((decimal_odds - 1) * 100)}"
    return f"{round(-100 / (decimal_odds - 1))}"


def kelly_stake(fair_p, cost, cfg):
    b = (1 / cost) - 1  # net profit per $1 staked
    if b <= 0:
        return 0.0
    f = (b * fair_p - (1 - fair_p)) / b
    f = max(0.0, f) * cfg["kelly_fraction"]
    f = min(f, cfg["max_bet_pct"])
    return round(cfg["bankroll"] * f, 2)


# ---------- scanning ----------

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def outcome_key(o):
    return (o["name"], o.get("point"))


def book_list(cfg):
    """Books to request: anchor, venues, other sharps, then display-only books. Max 10 = 1 credit/market."""
    return list(dict.fromkeys([cfg["sharp_book"], *cfg["venues"], *cfg.get("sharp_weights", {}),
                               *cfg.get("display_books", [])]))[:10]


def consensus_fair(books, mkey, anchor_outs, cfg, now):
    """Blend no-vig probabilities from every fresh sharp book that offers the same lines.
    Returns {outcome_key: (fair_p, n_books, spread)}; spread = max - min across books."""
    max_age = timedelta(minutes=cfg["max_line_age_minutes"])
    weights = cfg.get("sharp_weights") or {cfg["sharp_book"]: 1.0}
    keys = [outcome_key(o) for o in anchor_outs]
    per_book = {}
    for bk, w in weights.items():
        b = books.get(bk)
        m = b and next((m for m in b["markets"] if m["key"] == mkey), None)
        if not m or now - parse_time(m.get("last_update") or b.get("last_update") or now.isoformat()) > max_age:
            continue
        prices = {outcome_key(o): o["price"] for o in m["outcomes"]}
        if set(prices) != set(keys):
            continue  # different line or missing side: not comparable
        if sum(1 / x for x in prices.values()) - 1 > cfg["skip_overround_above"]:
            continue
        per_book[bk] = (w, dict(zip(keys, power_devig([prices[k] for k in keys]))))
    out = {}
    for k in keys:
        vals = [(w, f[k]) for w, f in per_book.values()]
        if not vals:
            continue
        p = sum(w * x for w, x in vals) / sum(w for w, _ in vals)
        xs = [x for _, x in vals]
        out[k] = (p, len(vals), max(xs) - min(xs))
    return out


def scan_event(ev, cfg, now, observe=None):
    start = parse_time(ev["commence_time"])
    mins_to_start = (start - now).total_seconds() / 60
    if mins_to_start < cfg["min_minutes_to_start"] or mins_to_start > cfg["max_hours_to_start"] * 60:
        return []

    books = {b["key"]: b for b in ev.get("bookmakers", [])}
    sharp = books.get(cfg["sharp_book"])
    if not sharp:
        return []
    max_age = timedelta(minutes=cfg["max_line_age_minutes"])

    hits = []
    for sm in sharp["markets"]:
        mkey = sm["key"]
        sharp_updated = sm.get("last_update") or sharp.get("last_update") or now.isoformat()
        if now - parse_time(sharp_updated) > max_age:
            continue
        outs = sm["outcomes"]
        if len(outs) < 2:
            continue
        overround = sum(1 / o["price"] for o in outs) - 1
        if overround > cfg["skip_overround_above"]:
            continue
        fair = consensus_fair(books, mkey, outs, cfg, now)

        for venue in cfg["venues"]:
            vb = books.get(venue)
            if not vb:
                continue
            vm = next((m for m in vb["markets"] if m["key"] == mkey), None)
            if not vm or now - parse_time(vm.get("last_update") or vb.get("last_update") or now.isoformat()) > max_age:
                continue
            for o in vm["outcomes"]:
                k = outcome_key(o)
                if k not in fair:
                    continue  # different line (e.g. spread -3.5 vs -4.5): not comparable
                p, n_books, spread = fair[k]
                agree = n_books >= cfg.get("consensus_min_books", 99) and spread <= cfg.get("consensus_max_spread", 0)
                bar = (cfg.get("min_ev_consensus", {}) if agree else cfg["min_ev"]).get(mkey, 0.03)
                if overround > cfg["wide_market_overround"]:
                    bar += cfg["wide_market_penalty"]
                cost = effective_cost(venue, o["price"], cfg)
                ev_pct = p / cost - 1
                pick = o["name"] + (f" {o['point']:+g}" if mkey == "spreads" else
                                    f" {o['point']:g}" if mkey == "totals" else "")
                if observe is not None:
                    observe.append({"sport": ev["sport_title"], "sport_key": ev["sport_key"],
                                    "event_id": ev["id"], "event": f"{ev['away_team']} @ {ev['home_team']}",
                                    "start": ev["commence_time"], "market": mkey, "pick": pick,
                                    "venue": venue, "price": o["price"], "fair_p": round(p, 4),
                                    "pinnacle_p": round(power_devig([x["price"] for x in outs])[
                                        [outcome_key(x) for x in outs].index(k)], 4),
                                    "n_books": n_books, "spread": round(spread, 4),
                                    "ev": round(ev_pct, 4), "bar": round(bar, 4),
                                    "mins_to_start": round(mins_to_start)})
                if ev_pct < bar:
                    continue
                if ev_pct > cfg["max_believable_ev"]:
                    print(f"  skipped (too good to be true, likely stale or rules mismatch): "
                          f"{ev['away_team']} @ {ev['home_team']} {mkey} {k} {venue} EV {ev_pct:.1%}",
                          file=sys.stderr)
                    continue
                hits.append({
                    "event_id": ev["id"], "sport_key": ev["sport_key"], "name": o["name"],
                    "point": o.get("point"), "sport": ev["sport_title"],
                    "event": f"{ev['away_team']} @ {ev['home_team']}",
                    "home": ev["home_team"], "away": ev["away_team"],
                    "start": start, "market": mkey, "pick": pick, "venue": venue,
                    "price": o["price"], "price_cents": round(100 / o["price"]),
                    "fair_p": p, "fair_odds": 1 / p, "ev": ev_pct, "bar": bar,
                    "n_books": n_books, "spread": spread, "sharp_updated": sharp_updated,
                    "stake": kelly_stake(p, cost, cfg),
                })
    return hits


# ---------- output ----------

def format_message(hits, now, remaining):
    local_tz = datetime.now().astimezone().tzinfo
    if not hits:
        return f"Edge Finder {now.astimezone(local_tz):%b %d}: no bets cleared the bar today."
    lines = [f"Edge Finder {now.astimezone(local_tz):%b %d}: {len(hits)} bet(s)"]
    for i, h in enumerate(hits, 1):
        t = h["start"].astimezone(local_tz).strftime("%a %-I:%M%p").replace(":00", "")
        lines.append(
            f"\n{i}. {VENUE_LABEL[h['venue']]}: {h['pick']} {MARKET_LABEL[h['market']]}"
            f"\n   {h['event']} ({h['sport']}, {t})"
            f"\n   Buy at or under {h['price_cents']}c ({to_american(h['price'])})"
            f" | fair {to_american(h['fair_odds'])} | EV {h['ev']:.1%}"
            f"\n   Bet ${h['stake']:.0f}"
        )
    lines.append("\nCheck the live price before buying. If it moved past the limit, skip it.")
    if remaining:
        lines.append(f"(API credits left: {remaining})")
    return "\n".join(lines)


def log_hits(hits, now):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["flagged_at", "sport", "event", "start", "market", "pick", "venue",
                        "price_decimal", "fair_prob", "ev", "bar", "stake"])
        for h in hits:
            w.writerow([now.isoformat(), h["sport"], h["event"], h["start"].isoformat(), h["market"],
                        h["pick"], h["venue"], h["price"], round(h["fair_p"], 4),
                        round(h["ev"], 4), h["bar"], h["stake"]])


def send_imessage(phone, text):
    script = [
        "on run argv",
        'tell application "Messages"',
        "set svc to 1st account whose service type = iMessage",
        "send (item 1 of argv) to participant (item 2 of argv) of svc",
        "end tell",
        "end run",
    ]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    subprocess.run(args + [text, phone], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of texting")
    ap.add_argument("--only-hits", action="store_true",
                    help="stay silent when nothing new qualifies (for frequent schedules)")
    a = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    key = load_api_key()
    now = datetime.now(timezone.utc)

    hits, remaining = [], None
    for sport in cfg["sports"]:
        events, rem = fetch_odds(key, sport, cfg)
        remaining = rem or remaining
        for ev in events:
            hits.extend(scan_event(ev, cfg, now))

    # One alert per pick: keep the better venue if both Kalshi and Polymarket qualify.
    best = {}
    for h in hits:
        k = (h["event"], h["market"], h["pick"])
        if k not in best or h["ev"] > best[k]["ev"]:
            best[k] = h
    hits = sorted(best.values(), key=lambda h: h["ev"], reverse=True)

    # Don't re-alert a pick already texted unless it still applies to a game that hasn't started.
    sent = json.loads(SENT_PATH.read_text()) if SENT_PATH.exists() else {}
    sent = {k: v for k, v in sent.items() if parse_time(v) > now}  # forget started games
    hits = [h for h in hits if "|".join((h["event"], h["market"], h["pick"])) not in sent]
    hits = hits[: cfg["max_alerts"]]

    log_hits(hits, now)
    msg = format_message(hits, now, remaining)
    print(msg)

    if a.only_hits and not hits:
        return
    if not a.dry_run:
        for h in hits:
            sent["|".join((h["event"], h["market"], h["pick"]))] = h["start"].isoformat()
        SENT_PATH.write_text(json.dumps(sent))
        import secrets_env
        phone = cfg.get("phone") or secrets_env.get("EF_PHONE")
        if not phone:
            sys.exit("No phone set (EF_PHONE in .env), so nothing was texted.")
        send_imessage(phone, msg)
        print("Texted.")


if __name__ == "__main__":
    main()
