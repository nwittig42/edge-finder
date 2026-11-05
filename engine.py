#!/usr/bin/env python3
"""Edge Finder engine: runs 24/7, paper-trades (or later, live-trades) +EV bets.

Loop:
  1. Free: list upcoming games per sport (The Odds API /events costs 0 credits).
  2. Paid: pull Pinnacle + Kalshi + Polymarket US odds only for sports with games
     coming up, at a pace that spreads the monthly credit budget evenly.
     Sports with a game in the next few hours are checked most often.
  3. Any pick that clears the EV bar becomes a paper bet, sized by quarter Kelly
     on that venue's paper balance, and is texted to you.
  4. Each scan also records Pinnacle's latest fair price for open bets, so the
     last one before kickoff is the closing line (CLV = did we beat it?).
  5. Finished games are graded from final scores; a daily recap is texted.

Run:  python3 engine.py            (forever)
      python3 engine.py --once     (one cycle, for testing; still texts)
      python3 engine.py --once --no-text
"""

import argparse
import csv
import json
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import dashboard
import learner
import live
import news
import secrets_env
import soccer_model
import webserver
from edge_finder import (book_list, consensus_fair, API, CONFIG_PATH, HERE, MARKET_LABEL, VENUE_LABEL, effective_cost,
                         kelly_stake, load_api_key, outcome_key, parse_time, power_devig,
                         scan_event, send_imessage, to_american)

STATE_PATH = HERE / "logs" / "paper_state.json"
LOG = HERE / "logs" / "engine.log"
SOON_HOURS = 6          # games starting within this window get the fastest checks
FAR_MULTIPLIER = 4      # other sports are checked this many times less often
SETTLE_EVERY_HOURS = 3
LEARN_EVERY_HOURS = 6
URGENT_MIN_GAP = 5     # minutes between news-triggered checks of the same sport


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def get(path, **params):
    params["apiKey"] = KEY
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        rem = r.headers.get("x-requests-remaining")
        if rem is not None:
            STATE["credits_remaining"] = int(float(rem))
        return json.load(r)


# ---------- state ----------

def load_state(cfg):
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text())
    else:
        s = {"balances": dict(cfg["bankroll_by_venue"]), "bets": [], "last_fetch": {},
             "last_settle": None, "last_recap": None, "credits_remaining": None}
    for v, amt in cfg["bankroll_by_venue"].items():
        s["balances"].setdefault(v, amt)
    return s


def save_state():
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=1, default=str))
    tmp.replace(STATE_PATH)


# ---------- budgeting ----------

def base_interval_minutes(cfg, soon_sports):
    """Minutes between checks of 'soon' sports so credits last to month end."""
    rem = STATE.get("credits_remaining")
    if rem is None:
        return cfg["min_interval_minutes"]
    now = datetime.now()
    month_end = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0)
    days_left = max((month_end - now).total_seconds() / 86400, 0.5)
    spendable = max(rem - cfg["reserve_credits"], 0)
    per_day = spendable / days_left
    cost = max(len(soon_sports), 1) * len(cfg["markets"])
    if per_day <= 0:
        return 24 * 60
    checks_per_day = per_day / cost
    return max(cfg["min_interval_minutes"], 24 * 60 / checks_per_day)


# ---------- closing line + paper bets ----------

def pinnacle_fair(ev, mkey, cfg):
    sharp = next((b for b in ev.get("bookmakers", []) if b["key"] == cfg["sharp_book"]), None)
    if not sharp:
        return {}
    m = next((m for m in sharp["markets"] if m["key"] == mkey), None)
    if not m or len(m["outcomes"]) < 2:
        return {}
    outs = m["outcomes"]
    return dict(zip([outcome_key(o) for o in outs], power_devig([o["price"] for o in outs])))


def model_fair(ev, cfg, cache):
    """Soccer score model fitted to Pinnacle's no-vig lines for this game (cached per cycle)."""
    if ev["id"] not in cache:
        fairs = {mk: pinnacle_fair(ev, mk, cfg) for mk in cfg["markets"]}
        cache[ev["id"]] = soccer_model.fit_checked(
            soccer_model.targets_from_fairs(fairs, ev["home_team"], ev["away_team"]))
    return cache[ev["id"]]


def update_closing(events, cfg, now):
    by_id = {e["id"]: e for e in events}
    models = {}
    for b in STATE["bets"] + STATE.get("tracked", []):
        if b["status"] != "open" or b["event_id"] not in by_id:
            continue
        if parse_time(b["start"]) <= now:
            continue
        ev = by_id[b["event_id"]]
        fair = pinnacle_fair(ev, b["market"], cfg)
        p = fair.get((b["name"], b["point"]))
        if not p and b.get("src") == "model":
            m = model_fair(ev, cfg, models)
            leg = m and soccer_model.leg_for(b["market"], b["name"], b["point"], ev["home_team"], ev["away_team"])
            p = leg and soccer_model.prob(m["grid"], (leg,))
        if p:
            b["closing_fair_p"] = round(p, 4)
            b["closing_at"] = now.isoformat()


def place_paper(h, cfg, now):
    key = f'{h["event_id"]}|{h["market"]}|{h["name"]}|{h["point"]}'
    if any(b["key"] == key for b in STATE["bets"]):
        return None
    venue = h["venue"]
    bal = STATE["balances"].get(venue, 0)
    cost = effective_cost(venue, h["price"], cfg)
    stake = kelly_stake(h["fair_p"], cost, {**cfg, "bankroll": bal})
    if stake < 1 or stake > bal:
        return None
    if h.get("size"):
        stake = min(stake, round(h["size"] * cost, 2))  # can't buy more than is for sale at this price
        if stake < 1:
            return None
    open_bets = [b for b in STATE["bets"] if b["status"] == "open"]
    if any(b["event_id"] == h["event_id"] for b in open_bets):
        return None  # one bet per game: ML + spread on the same team is mostly the same bet twice
    exposure = sum(b["stake"] for b in open_bets)
    total = sum(STATE["balances"].values()) + exposure
    if exposure + stake > cfg.get("max_open_exposure_pct", 0.25) * total:
        return None  # never have more than 25% of the bankroll riding at once
    STATE["balances"][venue] = round(bal - stake, 2)
    bet = {
        "key": key, "placed_at": now.isoformat(), "mode": cfg.get("mode", "paper"),
        "sport_key": h["sport_key"], "sport": h["sport"], "event_id": h["event_id"],
        "event": h["event"], "start": h["start"].isoformat(), "market": h["market"],
        "name": h["name"], "point": h["point"], "pick": h["pick"], "venue": venue,
        "price": h["price"], "cost": round(cost, 4), "stake": stake,
        "contracts": round(stake / cost, 2), "fair_p": round(h["fair_p"], 4),
        "ev": round(h["ev"], 4), "closing_fair_p": round(h["fair_p"], 4),
        "n_books": h.get("n_books"), "spread": round(h.get("spread", 0), 4), "bar": round(h["bar"], 4),
        "news_age_min": h.get("news_age_min"), "mins_to_start": round((h["start"] - now).total_seconds() / 60),
        "source": "live" if h.get("live") else "odds_api", "ref": h.get("ref"), "src": h.get("src"),
        "status": "open", "pnl": None,
    }
    STATE["bets"].append(bet)
    return bet


# ---------- observations (learning data) ----------

OBS_PATH = HERE / "logs" / "observations.csv"
OBS_FIELDS = ["seen_at", "sport", "event", "start", "market", "pick", "venue", "price",
              "fair_p", "pinnacle_p", "n_books", "spread", "ev", "bar", "mins_to_start", "event_id"]


def record_observations(sport, obs, now):
    """Every venue-vs-Pinnacle comparison goes to a CSV: the raw data for tuning bars per
    sport/market/venue/time-to-start. The dashboard 'radar' shows the closest ones."""
    new = not OBS_PATH.exists()
    with OBS_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for o in obs:
            w.writerow({**o, "seen_at": now.isoformat()})
    radar = STATE.setdefault("radar", {})
    radar[sport] = {"at": now.isoformat(),
                    "rows": sorted(obs, key=lambda o: -o["ev"])[:15],
                    "count": len(obs)}


def board_event(ev, cfg):
    """Compact odds-screen record: every book's price per outcome, plus Pinnacle no-vig fair."""
    markets = {}
    for mkey in cfg["markets"]:
        bks = {b["key"]: b for b in ev.get("bookmakers", [])}
        pm = bks.get(cfg["sharp_book"]) and next((m for m in bks[cfg["sharp_book"]]["markets"] if m["key"] == mkey), None)
        cons = consensus_fair(bks, mkey, pm["outcomes"], cfg, datetime.now(timezone.utc)) if pm else {}
        fair = {k: v[0] for k, v in cons.items()}
        rows, updated = {}, {}
        for b in ev.get("bookmakers", []):
            m = next((m for m in b["markets"] if m["key"] == mkey), None)
            if not m:
                continue
            updated[b["key"]] = m.get("last_update", b.get("last_update"))
            for o in m["outcomes"]:
                k = outcome_key(o)
                if k not in fair and b["key"] not in cfg["venues"]:
                    continue  # skip alt lines nobody we trade on or price against offers
                r = rows.setdefault(k, {"name": o["name"], "point": o.get("point"),
                                        "fair": round(fair[k], 4) if k in fair else None,
                                        "n": cons[k][1] if k in cons else 0,
                                        "spread": round(cons[k][2], 4) if k in cons else None, "prices": {}})
                r["prices"][b["key"]] = o["price"]
        if rows:
            markets[mkey] = {"rows": list(rows.values()), "updated": updated}
    out = {"id": ev["id"], "sport": ev["sport_title"], "away": ev["away_team"], "home": ev["home_team"],
           "start": ev["commence_time"], "markets": markets}
    if ev.get("sport_key", "").startswith("soccer"):
        soccer_model.fill_board_event(out)  # fair prices for venue lines the sharps don't quote
    return out


# ---------- grading ----------

def grade(b, game):
    scores = {s["name"]: float(s["score"]) for s in game.get("scores") or []}
    if len(scores) < 2:
        return None
    home, away = game["home_team"], game["away_team"]
    if home not in scores or away not in scores:
        return None
    hs, as_ = scores[home], scores[away]
    m, name, pt = b["market"], b["name"], b["point"]
    if m == "h2h":
        if name == "Draw":
            return "won" if hs == as_ else "lost"
        mine, theirs = (hs, as_) if name == home else (as_, hs)
        return "won" if mine > theirs else "lost" if mine < theirs else "push"
    if m == "spreads":
        mine, theirs = (hs, as_) if name == home else (as_, hs)
        diff = mine + pt - theirs
        return "won" if diff > 0 else "lost" if diff < 0 else "push"
    if m == "totals":
        total = hs + as_
        if total == pt:
            return "push"
        return "won" if (total > pt) == (name == "Over") else "lost"
    return None


def settle(cfg, now):
    open_bets = [b for b in STATE["bets"] + STATE.get("tracked", [])
                 if b["status"] == "open" and parse_time(b["start"]) + timedelta(hours=2.5) < now]
    if not open_bets:
        return []
    done = []
    for sport in sorted({b["sport_key"] for b in open_bets}):
        try:
            games = {g["id"]: g for g in get(f"/sports/{sport}/scores", daysFrom=3)}
        except Exception as e:
            log(f"scores {sport} failed: {e}")
            continue
        for b in open_bets:
            g = games.get(b["event_id"])
            if b["sport_key"] != sport or not g or not g.get("completed"):
                continue
            result = grade(b, g)
            if not result:
                continue
            b["status"] = result
            b["pnl"] = round(b["contracts"] - b["stake"], 2) if result == "won" else \
                -b["stake"] if result == "lost" else 0.0
            b["settled_at"] = now.isoformat()
            if b.get("tier") != "track":   # the tracking list never touches the paper bankroll
                STATE["balances"][b["venue"]] = round(STATE["balances"][b["venue"]] + b["stake"] + b["pnl"], 2)
            done.append(b)
    return done


# ---------- messages ----------

def clv(b):
    """Closing line value: closing fair prob vs what we effectively paid."""
    return b["closing_fair_p"] / b["cost"] - 1


def bet_lines(b):
    local = parse_time(b["start"]).astimezone()
    t = local.strftime("%a %-I:%M%p")
    return (f"{VENUE_LABEL[b['venue']]}: {b['pick']} {MARKET_LABEL[b['market']]}\n"
            f"   {b['event']} ({b['sport']}, {t})\n"
            f"   Bought at {round(100 / b['price'])}c ({to_american(b['price'])}) | "
            f"fair {to_american(1 / b['fair_p'])} | EV {b['ev']:.1%}\n"
            f"   Stake ${b['stake']:.2f}" + (" (live price)" if b.get("source") == "live" else ""))


def text(cfg, msg, no_text):
    log("MESSAGE:\n" + msg)
    phone = cfg.get("phone") or secrets_env.get("EF_PHONE")
    if no_text or not phone:
        return
    hour = datetime.now().hour
    q0, q1 = cfg.get("quiet_hours", [1, 8])
    if q0 <= hour < q1:
        STATE.setdefault("held_messages", []).append(msg)
        return
    try:
        held = STATE.pop("held_messages", [])
        if held:
            send_imessage(phone, "While you were asleep:\n\n" + "\n\n".join(held))
        send_imessage(phone, msg)
    except Exception as e:
        log(f"iMessage failed: {e}")


def recap(cfg, now):
    since = now - timedelta(days=1)
    settled = [b for b in STATE["bets"] if b.get("settled_at") and parse_time(b["settled_at"]) > since]
    allset = [b for b in STATE["bets"] if b["status"] in ("won", "lost", "push")]
    open_n = sum(b["status"] == "open" for b in STATE["bets"])
    pnl_day = sum(b["pnl"] for b in settled)
    pnl_all = sum(b["pnl"] for b in allset)
    staked_all = sum(b["stake"] for b in allset) or 1
    clvs = [clv(b) for b in allset]
    beat = sum(c > 0 for c in clvs)
    bal = " | ".join(f"{VENUE_LABEL[v]} ${x:,.2f}" for v, x in STATE["balances"].items())
    lines = [f"Edge Finder daily recap ({cfg.get('mode', 'paper').upper()})",
             f"Last 24h: {len(settled)} settled, P&L ${pnl_day:+.2f}",
             f"All time: {len(allset)} settled, P&L ${pnl_all:+.2f} (ROI {pnl_all / staked_all:+.1%})"]
    if clvs:
        lines.append(f"Beat closing line: {beat}/{len(clvs)} (avg CLV {sum(clvs) / len(clvs):+.1%})")
    pol = learner.load_policy()
    if pol:
        segs = pol["segments"].values()
        lines.append(f"Learning: {pol['picks']} graded picks, {sum(v.get('bar') is not None for v in segs)} "
                     f"learned bars, {sum(v['blocked'] for v in segs)} blocked segments")
    lines += [f"Open bets: {open_n}", f"Balances: {bal}",
              f"API credits left: {STATE.get('credits_remaining')}"]
    return "\n".join(lines)


def process_hits(hits, cfg, now, no_text):
    """Shared by the odds-check cycle and the live tick: live-price override, news guard, place, text."""
    # Where a live venue quote exists, it replaces the Odds API's delayed copy of that price.
    checked = []
    for h in hits:
        if not h.get("live"):
            q = live.live_quote(h["event_id"], h["market"], h["name"], h["point"], h["venue"])
            if q:
                cost = effective_cost(h["venue"], 1 / q["cost_raw"], cfg)
                ev_pct = h["fair_p"] / cost - 1
                if ev_pct < h["bar"]:
                    continue  # the live price no longer has the edge
                h.update(price=1 / q["cost_raw"], ev=ev_pct, live=True, size=q["size"], ref=q["ref"])
        checked.append(h)
    best = {}
    for h in checked:
        k = (h["event_id"], h["market"], h["name"], h["point"])
        if k not in best or h["ev"] > best[k]["ev"]:
            best[k] = h
    # Learned policy (learner.py): segments whose edge doesn't survive to the close are blocked,
    # others get a bar fitted from their own closing-line history. Tracking keeps sampling everything.
    for k, h in list(best.items()):
        why = learner.gate(h)
        if why:
            del best[k]
            if not h.get("live"):
                log(f"policy skip ({why}): {h['event']} {h['pick']} {h['venue']} EV {h['ev']:.1%}")
    # Stale-fair guard: news on either team newer than Pinnacle's last move = wait.
    for k, h in list(best.items()):
        t = news.latest_team_news(STATE, h["sport_key"], h["home"], h["away"])
        h["news_age_min"] = round((now - t).total_seconds() / 60) if t else None
        if t and t > parse_time(h["sharp_updated"]):
            log(f"held (news newer than Pinnacle line): {h['event']} {h['pick']} {h['venue']} EV {h['ev']:.1%}")
            del best[k]
    placed = [b for h in sorted(best.values(), key=lambda h: -h["ev"])
              if (b := place_paper(h, cfg, now))]
    if placed:
        head = f"PAPER BET{'S' if len(placed) > 1 else ''} ({len(placed)})"
        text(cfg, head + "\n\n" + "\n\n".join(bet_lines(b) for b in placed), no_text)
        save_state()
    return placed


def track(cands, cfg, now):
    """Paper tracking list: one flat-stake entry per pick (best venue at first sighting). No texts,
    no bankroll. Graded and CLV-checked like real paper bets, to learn which EV levels beat the close."""
    tracked = STATE.setdefault("tracked", [])
    seen = {t["key"] for t in tracked}
    best = {}
    for h in cands:
        k = f'{h["event_id"]}|{h["market"]}|{h["name"]}|{h["point"]}'
        if k not in seen and (k not in best or h["ev"] > best[k]["ev"]):
            best[k] = h
    stake = cfg.get("track_stake", 10)
    for k, h in best.items():
        cost = effective_cost(h["venue"], h["price"], cfg)
        tracked.append({**{x: h[x] for x in ("sport_key", "sport", "event_id", "event", "start", "market", "name",
                                              "point", "pick", "venue", "price", "n_books", "spread", "bar",
                                              "fair_age_min", "mins_to_start", "ref")},
                        "key": k, "tier": "track", "placed_at": now.isoformat(), "cost": round(cost, 4),
                        "stake": stake, "contracts": round(stake / cost, 2), "fair_p": round(h["fair_p"], 4),
                        "ev": round(h["ev"], 4), "cleared_bar": h["ev"] >= h["bar"],
                        "closing_fair_p": round(h["fair_p"], 4), "src": h.get("src"), "status": "open",
                        "pnl": None})
    if best:
        log(f"tracking: +{len(best)} pick(s), {len(tracked)} total")
        save_state()


def get_event_odds(sport, event_id, markets=("h2h",)):
    """1 credit per market: fresh prices from all books for a single game (used by the live re-check)."""
    try:
        return get(f"/sports/{sport}/events/{event_id}/odds", bookmakers=",".join(book_list(STATE_CFG[0])),
                   markets=",".join(markets), oddsFormat="decimal")
    except Exception as e:
        log(f"event odds {event_id} failed: {e}")
        return None


# ---------- main cycle ----------

def cycle(cfg, no_text, force=False):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=cfg["max_hours_to_start"])

    # 1. Free: which sports have games coming up, and how soon.
    soonest = {}
    cache = STATE.setdefault("events_cache", {})
    for sport in cfg["sports"]:
        c = cache.get(sport)
        if force or not c or now - parse_time(c["at"]) > timedelta(minutes=10):
            try:
                evs = get(f"/sports/{sport}/events")
            except Exception as e:
                log(f"events {sport} failed: {e}")
                continue
            c = cache[sport] = {"at": now.isoformat(), "starts": [e["commence_time"] for e in evs]}
        starts = [parse_time(x) for x in c["starts"]]
        upcoming = [s for s in starts if now < s < horizon]
        if upcoming:
            soonest[sport] = min(upcoming)
        open_live = any(b["status"] == "open" and b["sport_key"] == sport and parse_time(b["start"]) > now
                        for b in STATE["bets"])
        if open_live and sport not in soonest:
            soonest[sport] = horizon

    soon = [s for s, t in soonest.items() if t - now < timedelta(hours=SOON_HOURS)]
    base = base_interval_minutes(cfg, soon)

    # 1b. Free: injury news. A change makes that sport due for a check right away.
    urgent = STATE.setdefault("urgent", {})
    for sp in news.poll(STATE, now, log):
        if sp in soonest:
            urgent[sp] = now.isoformat()
    day = datetime.now().date().isoformat()
    if STATE.get("urgent_day") != day:
        STATE["urgent_day"], STATE["urgent_used"] = day, 0

    # 2. Paid: fetch only sports that are due.
    hits, all_events, fetched = [], [], []
    for sport, t in soonest.items():
        every = base if sport in soon else base * FAR_MULTIPLIER
        last = STATE["last_fetch"].get(sport)
        since = (now - parse_time(last)).total_seconds() / 60 if last else 1e9
        is_urgent = (sport in urgent and since >= URGENT_MIN_GAP
                     and STATE["urgent_used"] < cfg.get("max_news_checks_per_day", 40))
        if not force and since < every and not is_urgent:
            continue
        if is_urgent and since < every:
            STATE["urgent_used"] += 1
            log(f"news-triggered check: {sport}")
        urgent.pop(sport, None)
        params = {"bookmakers": ",".join(book_list(cfg)),
                  "markets": ",".join(cfg["markets"]), "oddsFormat": "decimal"}
        try:
            events = get(f"/sports/{sport}/odds", **params)
        except urllib.error.HTTPError as e:
            if e.code != 422:
                log(f"odds {sport} HTTP {e.code}")
                continue
            events = get(f"/sports/{sport}/odds", **{**params, "markets": "h2h"})
        except Exception as e:
            log(f"odds {sport} failed: {e}")
            continue
        STATE["last_fetch"][sport] = now.isoformat()
        fetched.append(sport)
        all_events += events
        obs = []
        for ev in events:
            hits += scan_event(ev, cfg, now, observe=obs)
        record_observations(sport, obs, now)
        STATE.setdefault("board", {})[sport] = {"at": now.isoformat(),
                                                "events": [board_event(ev, cfg) for ev in events]}

    update_closing(all_events, cfg, now)

    # 3. Paper bets.
    placed = process_hits(hits, cfg, now, no_text)

    # 4. Grade finished games every few hours.
    last_settle = STATE.get("last_settle")
    if force or not last_settle or now - parse_time(last_settle) > timedelta(hours=SETTLE_EVERY_HOURS):
        STATE["last_settle"] = now.isoformat()
        done = settle(cfg, now)
        if done:
            log(f"settled {len(done)} bets")

    # 4b. Re-learn betting rules from closing-line results.
    last_learn = STATE.get("last_learn")
    if force or not last_learn or now - parse_time(last_learn) > timedelta(hours=LEARN_EVERY_HOURS):
        STATE["last_learn"] = now.isoformat()
        try:
            pol = learner.run(STATE, now)
            blocked = [k for k, v in pol["segments"].items() if v["blocked"] and not k.endswith("|model")]
            ruled = [k for k, v in pol["segments"].items() if v.get("bar") is not None]
            log(f"learner: {pol['picks']} graded picks, {len(ruled)} learned bars, blocked {blocked or 'none'}")
        except Exception:
            log("learner failed:\n" + traceback.format_exc())

    # 5. Daily recap.
    local = datetime.now()
    today = local.date().isoformat()
    if local.hour >= cfg.get("recap_hour", 9) and STATE.get("last_recap") != today:
        STATE["last_recap"] = today
        text(cfg, recap(cfg, now), no_text)

    if fetched or force:
        STATE["last_cycle_fetch"] = now.isoformat()
    STATE["last_cycle"] = now.isoformat()
    STATE["interval_min"] = round(base)
    try:
        dashboard.render(STATE, cfg)
        dashboard.render_odds(STATE, cfg)
    except Exception:
        log("dashboard render failed:\n" + traceback.format_exc())
    log(f"cycle: fetched {fetched or 'none'}, {len(hits)} hits, {len(placed)} new bets, "
        f"interval {base:.0f}m, credits {STATE.get('credits_remaining')}")
    save_state()


def main():
    global KEY, STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-text", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(exist_ok=True)
    KEY = load_api_key()
    cfg = json.loads(CONFIG_PATH.read_text())
    STATE = load_state(cfg)
    STATE_CFG[0] = cfg
    if cfg.get("mode", "paper") != "paper":
        sys.exit("Live mode isn't built yet. Set \"mode\": \"paper\" in config.json.")
    if a.once:
        cycle(cfg, a.no_text, force=True)
        return
    log("engine started (paper mode)")
    webserver.start(lambda: STATE, log)
    started = STATE.get("start_texted")
    if not started or datetime.now(timezone.utc) - parse_time(started) > timedelta(hours=12):
        text(cfg, "Edge Finder engine is running (paper mode). You'll get a text for each paper bet "
                  "and a recap every morning at 9.", a.no_text)
        STATE["start_texted"] = datetime.now(timezone.utc).isoformat()
    save_state()
    last_cycle = 0
    while True:
        cfg = json.loads(CONFIG_PATH.read_text())  # picks up config edits without restart
        STATE_CFG[0] = cfg
        if time.time() - last_cycle >= 60:
            last_cycle = time.time()
            try:
                cycle(cfg, a.no_text)
            except Exception:
                log("cycle crashed:\n" + traceback.format_exc())
        try:
            now = datetime.now(timezone.utc)
            process_hits(live.tick(STATE, cfg, now, get_event_odds, log), cfg, now, a.no_text)
            track(live.track_candidates(STATE, cfg, now), cfg, now)
        except Exception:
            log("live tick crashed:\n" + traceback.format_exc())
        time.sleep(10)


KEY = None
STATE = {}
STATE_CFG = [None]

if __name__ == "__main__":
    main()
