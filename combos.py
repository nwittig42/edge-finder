"""Same-game soccer combo pricer (Kalshi "combos" = parlays built on demand, priced by market makers via RFQ).

For each upcoming soccer game in a league Kalshi allows in combos, fit the Dixon-Coles score model to the
sharp no-vig lines (soccer_model.py) and price every 2-leg combo of result / total / handicap / BTTS legs.

"lift" = joint probability / product of the legs. Lift > 1 means the legs win together more often than
independent pricing assumes, i.e. a parlay priced leg-by-leg would be too cheap. "buy_below" is the most
you can pay per $1 contract (Kalshi taker fee included) and still have MIN_EV.

No quotes are requested: getting a price means sending an RFQ to Kalshi's market makers, which is off
until you turn it on. This produces the price targets.

Run: python3 combos.py            (prints the top combos, writes logs/combos.json)
"""

import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import soccer_model as sm

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "logs" / "paper_state.json"
OUT_PATH = HERE / "logs" / "combos.json"

# Odds API sport keys whose games Kalshi accepts as combo legs (from the KXMVESPORTSMULTIGAMEEXTENDED
# collection, Nov 5 2026: KXEPL*, KXEFLCHAMPIONSHIP*, KXMLS*, KXUEL*, KXUECL* with GAME/SPREAD/TOTAL/BTTS).
COMBO_SPORTS = {"soccer_epl", "soccer_efl_champ", "soccer_usa_mls", "soccer_uefa_europa_league",
                "soccer_uefa_europa_conference_league"}
MIN_EV = 0.03
KALSHI_FEE = 0.07
MIN_LIFT = 1.08
MIN_JOINT = 0.08

LEGS = [("result", "home"), ("result", "draw"), ("result", "away"),
        ("total", "over", 1.5), ("total", "under", 1.5), ("total", "over", 2.5), ("total", "under", 2.5),
        ("total", "over", 3.5), ("total", "under", 3.5),
        ("btts", "yes"), ("btts", "no"),
        ("handicap", "home", -1.5), ("handicap", "away", -1.5)]


def leg_label(leg, home, away):
    team = {"home": home, "away": away}
    if leg[0] == "result":
        return "Draw" if leg[1] == "draw" else f"{team[leg[1]]} win"
    if leg[0] == "total":
        return f"{leg[1].title()} {leg[2]:g}"
    if leg[0] == "handicap":
        return f"{team[leg[1]]} by 2+"
    if leg[0] == "btts":
        return f"BTTS {leg[1]}"
    return str(leg)


def buy_below(fair, min_ev=MIN_EV, fee=KALSHI_FEE):
    """Highest price q with fair / (q + fee*q*(1-q)) - 1 >= min_ev (bisection)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        q = (lo + hi) / 2
        if fair / (q + fee * q * (1 - q)) - 1 >= min_ev:
            lo = q
        else:
            hi = q
    return lo


def price_event(ev):
    m = sm.fit_checked(sm.targets_from_board(ev))
    if not m:
        return None
    g = m["grid"]
    single = {leg: sm.prob(g, (leg,)) for leg in LEGS}
    out = []
    for a, b in itertools.combinations(LEGS, 2):
        joint = sm.prob(g, (a, b))
        lo = min(single[a], single[b])
        if joint < MIN_JOINT or joint > lo - 0.01:
            continue  # rare, or one leg (nearly) implies the other: not a real parlay
        naive = single[a] * single[b]
        out.append({"legs": [leg_label(a, ev["home"], ev["away"]), leg_label(b, ev["home"], ev["away"])],
                    "raw_legs": [list(a), list(b)], "fair": round(joint, 4), "naive": round(naive, 4),
                    "lift": round(joint / naive, 3), "buy_below_cents": round(100 * buy_below(joint), 1),
                    "naive_cents": round(100 * naive, 1)})
    return {"model": {"lh": round(m["lh"], 3), "la": round(m["la"], 3), "rho": round(m["rho"], 3),
                      "rmse": round(m["rmse"], 4)}, "combos": out}


def run(state=None, now=None, write=True):
    now = now or datetime.now(timezone.utc)
    if state is None:
        state = json.loads(STATE_PATH.read_text())
    games = []
    for sport, b in state.get("board", {}).items():
        if sport not in COMBO_SPORTS:
            continue
        for ev in b["events"]:
            start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
            if start <= now:
                continue
            p = price_event(ev)
            if p:
                games.append({"sport": ev["sport"], "event": f"{ev['away']} @ {ev['home']}", "start": ev["start"],
                              "odds_at": b["at"], **p})
    top = sorted(((g, c) for g in games for c in g["combos"] if c["lift"] >= MIN_LIFT),
                 key=lambda gc: -gc[1]["lift"] * math.sqrt(gc[1]["fair"]))
    result = {"generated_at": now.isoformat(), "games": len(games),
              "top": [{"event": g["event"], "sport": g["sport"], "start": g["start"], **c} for g, c in top[:60]],
              "all": games}
    if write:
        OUT_PATH.write_text(json.dumps(result, indent=1))
    return result


if __name__ == "__main__":
    r = run()
    print(f"{r['games']} combo-eligible games priced. Top correlated 2-leg combos "
          f"(buy below = max Kalshi price for {MIN_EV:.0%} EV after fees):\n")
    for c in r["top"][:20]:
        print(f"  {c['event'][:42]:42s} {' + '.join(c['legs'])[:44]:44s} fair {100 * c['fair']:4.1f}c "
              f"(naive {c['naive_cents']:4.1f}c, lift {c['lift']:.2f}) -> buy below {c['buy_below_cents']:.1f}c")
