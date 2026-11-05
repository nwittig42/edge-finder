"""Go-live monitor, computed live from local files for the /monitor page and /api/monitor.
Same checks as MONITOR_PROMPT.md: health, volume, CLV verdict, EV spread, P&L vs expected,
concentration, go-live checklist. Read only: never edits config, never spends API credits."""

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from edge_finder import HERE, parse_time

LOGS = HERE / "logs"
TARGET_BETS, TARGET_DAYS, HARD_STOP_N = 150, 21, 100
MIN_CLV, MIN_BEAT, MAX_HIGH_EV = 0.015, 0.55, 0.25
MONTHLY_CREDITS = 20000
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")


def _clv(b):
    return b["closing_fair_p"] / b["cost"] - 1


def valid_close(b):
    """Close must exist and be taken within 60 min of the start, or the 'close' is really an early line."""
    if not b.get("closing_at"):
        return False
    return (parse_time(b["start"]) - parse_time(b["closing_at"])).total_seconds() <= 3600


def _log_lines():
    """(local datetime, text) for each timestamped engine.log line."""
    f = LOGS / "engine.log"
    out = []
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            m = TS.match(line)
            if m:
                out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), line))
            elif out and line.strip():
                out.append((out[-1][0], line))  # continuation (tracebacks, message bodies)
    return out


def clv_stats(bets):
    c = [_clv(b) for b in bets]
    if not c:
        return {"n": 0, "avg": None, "median": None, "beat": None}
    return {"n": len(c), "avg": sum(c) / len(c), "median": statistics.median(c),
            "beat": sum(x > 0 for x in c) / len(c)}


def split(bets, field):
    g = defaultdict(list)
    for b in bets:
        g[b.get(field) or "?"].append(b)
    return {k: clv_stats(v) for k, v in sorted(g.items())}


def compute(state, cfg):
    now = datetime.now(timezone.utc)
    now_local = datetime.now()
    bets = state.get("bets", [])
    settled = [b for b in bets if b["status"] in ("won", "lost", "push")]
    open_bets = [b for b in bets if b["status"] == "open"]
    valid = [b for b in settled if valid_close(b)]
    bad = [b for b in settled if not valid_close(b)]
    week = [b for b in valid if (now - parse_time(b["placed_at"])).days < 7]

    # Health
    lines = _log_lines()
    first_seen = lines[0][0] if lines else now_local
    if bets:
        first_bet = min(parse_time(b["placed_at"]) for b in bets).astimezone().replace(tzinfo=None)
        first_seen = min(first_seen, first_bet)
    days_running = (now_local - first_seen).total_seconds() / 86400
    day_ago = now_local - timedelta(hours=24)
    errors = [t for d, t in lines if d >= day_ago and re.search(r"traceback|error|failed|exception", t, re.I)]
    credit_pts = [(d, int(m.group(1))) for d, t in lines
                  if (m := re.search(r"credits (\d+)", t)) and d >= day_ago]
    burn = None
    if len(credit_pts) >= 2 and (credit_pts[-1][0] - credit_pts[0][0]).total_seconds() > 3600:
        hrs = (credit_pts[-1][0] - credit_pts[0][0]).total_seconds() / 3600
        burn = max(credit_pts[0][1] - credit_pts[-1][1], 0) / hrs * 24
    credits = state.get("credits_remaining")
    nxt = (now_local.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0)
    days_left_month = (nxt - now_local).total_seconds() / 86400
    runout_days = credits / burn if burn and credits else None
    last = state.get("last_cycle")
    cycle_age = (now - parse_time(last)).total_seconds() / 60 if last else None

    # Volume
    span_days = max(days_running, 1 / 24)
    placed_per_day = len(bets) / span_days
    settled_per_day = len(valid) / span_days
    need = max(TARGET_BETS - len(valid), 0)
    eta_days = need / settled_per_day if settled_per_day else None
    by = lambda f: dict(sorted(Counter(b.get(f) or "?" for b in bets).items()))

    # EV distribution
    buckets = {"2-4%": 0, "4-6%": 0, "6-8%": 0, "8%+": 0}
    for b in bets:
        e = b["ev"]
        buckets["8%+" if e >= .08 else "6-8%" if e >= .06 else "4-6%" if e >= .04 else "2-4%"] += 1
    high_share = buckets["8%+"] / len(bets) if bets else None

    # P&L vs expected
    pnl = sum(b["pnl"] for b in settled)
    exp = sum(b["stake"] * b["ev"] for b in settled)
    var = 0.0
    for b in settled:
        p = b.get("fair_p") or min(b["cost"] * (1 + b["ev"]), .999)
        var += (b["stake"] * math.sqrt((1 - p) / p)) ** 2
    sd = math.sqrt(var)
    z = (pnl - exp) / sd if sd else None

    # Concentration: share of total CLV dollars (stake x CLV) by segment
    conc = {}
    tot = sum(max(b["stake"] * _clv(b), 0) for b in valid)
    for f in ("sport", "market", "venue"):
        g = defaultdict(float)
        for b in valid:
            g[b.get(f) or "?"] += max(b["stake"] * _clv(b), 0)
        if tot and g:
            k, v = max(g.items(), key=lambda kv: kv[1])
            conc[f] = {"top": k, "share": v / tot}

    # Checklist
    allc = clv_stats(valid)
    manual = {}
    mf = LOGS / "manual_checks.json"
    if mf.exists():
        try:
            manual = json.loads(mf.read_text())
        except ValueError:
            pass

    def st(ok, ready):
        return "PASS" if ready and ok else "FAIL" if ready else "NOT YET"

    enough = len(valid) >= 30
    checks = [
        ("21+ days running and 150+ settled bets with a valid close",
         st(True, days_running >= TARGET_DAYS and len(valid) >= TARGET_BETS),
         f"{days_running:.1f}/{TARGET_DAYS} days, {len(valid)}/{TARGET_BETS} bets"),
        ("Avg CLV at least +1.5% and beat-close over 55%",
         st(enough and allc["avg"] >= MIN_CLV and allc["beat"] > MIN_BEAT, enough),
         f"avg {allc['avg']:+.1%}, beat {allc['beat']:.0%}" if allc["n"] else "no settled bets yet"),
        ("Under 25% of bets at 8%+ EV",
         st(bool(bets) and high_share < MAX_HIGH_EV, len(bets) >= 20),
         f"{high_share:.0%} of {len(bets)}" if bets else "no bets yet"),
        ("P&L within 2 SD of expected (or above)",
         st(z is not None and z > -2, len(settled) >= 30),
         f"z = {z:+.2f}" if z is not None else "no settled bets yet"),
        ("Fillability spot-checked on ~20 alerts", manual.get("fillability", "NOT YET"),
         "needs your manual check"),
        ("Fees confirmed by one real $1-2 trade per venue", manual.get("fees", "NOT YET"),
         "needs your manual check"),
    ]

    # Verdict line
    hard_stop = len(valid) >= HARD_STOP_N and allc["avg"] is not None and allc["avg"] <= 0
    if hard_stop:
        verdict = (f"HARD STOP: no edge after {len(valid)} bets, avg CLV {allc['avg']:+.1%}. Likely causes: stale "
                   f"prediction-market prices, max_line_age_minutes too loose, fees understated, or devig method.")
        tone = "neg"
    elif all(c[1] == "PASS" for c in checks):
        verdict = "READY TO GO LIVE SMALL: $5-10 bets (about 25% of paper size) for 100 bets, then compare fills."
        tone = "pos"
    else:
        on_track = allc["avg"] is not None and allc["avg"] >= MIN_CLV
        verdict = (f"NOT READY: {len(valid)}/{TARGET_BETS} bets"
                   + (f", avg CLV {allc['avg']:+.1%} ({'on track' if on_track else 'below target'})"
                      if allc["n"] else ", no settled bets yet")
                   + (f", {len(bad)} data issue{'s' * (len(bad) != 1)}" if bad else ""))
        tone = "warn"
    low_volume = days_running >= 4 and placed_per_day < 5

    return {
        "verdict": verdict, "tone": tone, "hard_stop": hard_stop, "low_volume": low_volume,
        "health": {"cycle_age_min": cycle_age, "errors_24h": errors[-10:], "n_errors_24h": len(errors),
                   "credits": credits, "burn_per_day": burn, "runout_days": runout_days,
                   "days_left_month": days_left_month, "days_running": days_running},
        "volume": {"placed": len(bets), "settled": len(settled), "valid": len(valid), "open": len(open_bets),
                   "placed_per_day": placed_per_day, "eta_days": eta_days,
                   "by_sport": by("sport"), "by_market": by("market"), "by_venue": by("venue")},
        "clv": {"all": allc, "last7": clv_stats(week), "by_venue": split(valid, "venue"),
                "by_market": split(valid, "market"), "by_sport": split(valid, "sport"),
                "excluded": [{"pick": b["pick"], "event": b["event"], "closing_at": b.get("closing_at"),
                              "start": b["start"], "clv": _clv(b)} for b in bad]},
        "ev": {"buckets": buckets, "high_share": high_share},
        "pnl": {"actual": pnl, "expected": exp, "sd": sd, "z": z},
        "concentration": conc,
        "checklist": [{"item": a, "status": s, "detail": d} for a, s, d in checks],
    }


def history():
    f = LOGS / "monitor_history.md"
    return f.read_text() if f.exists() else ""
