"""The learning loop: turns closing-line results into betting rules the engine follows.

Closing line value (CLV) is the label: did the sharp market's final fair price end up better than what
we paid? It needs no game result, so it accumulates fast and converges far quicker than win/loss.

Per segment (venue x sport group x market x fair source) it fits a small shrunk regression:

    CLV = a + b * EV_at_placement

b is "edge retention" (how much of the EV we see survives to the close; 1.0 = all of it, 0 = it was an
illusion), a is drift that does not depend on EV. Both are pulled toward a prior (a=0, b=PRIOR_B) with
the weight of PRIOR_N pseudo-bets, so thin segments barely move and big ones speak for themselves.

From the fit it sets each segment's EV bar so that expected CLV >= TARGET_CLV, and blocks segments
where retention is near zero. Rules are recomputed from all data on every run, so a segment can earn
its way back. Only real paper bets are gated; the tracking list keeps sampling everything, which is
what lets a blocked segment be re-tested.

Data: paper bets + tracking list (state) and every logged comparison in logs/observations.csv,
labelled with that pick's last pre-game fair price.

Run by the engine every few hours; `python3 learner.py` prints the current report.
"""

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OBS_PATH = HERE / "logs" / "observations.csv"
STATE_PATH = HERE / "logs" / "paper_state.json"
POLICY_PATH = HERE / "logs" / "policy.json"

PRIOR_B = 0.6        # assume 60% of seen edge survives to the close until data says otherwise
PRIOR_N = 25         # prior is worth this many bets
TARGET_CLV = 0.01    # bet only where expected CLV (our best estimate of true EV) is at least 1%
MIN_N_RULE = 30      # unique picks before a segment's own rule replaces the default bars
MIN_BAR, MAX_BAR = 0.015, 0.10
BLOCK_B = 0.15       # retention below this with enough data = the edge isn't real here
MAX_CLOSE_MIN = 180  # an observation counts as "closing" only if taken this close to kickoff

SOCCER_WORDS = ("soccer", "league", "liga", "bundesliga", "serie a", "ligue", "premier", "mls", "championship",
                "eredivisie", "epl", "uefa", "primeira", "fa cup", "copa", "liga mx")


def _t(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def group(sport, sport_key=None):
    if (sport_key or "").startswith("soccer") or any(w in (sport or "").lower() for w in SOCCER_WORDS):
        return "Soccer"
    return sport or "?"


def seg_key(venue, sport, market, src=None, sport_key=None):
    return f"{venue}|{group(sport, sport_key)}|{market}|{src or 'sharp'}"


# ---------- data ----------

def from_state(state):
    out = {}
    for b in state.get("bets", []) + state.get("tracked", []):
        if not b.get("closing_at") or not b.get("cost"):
            continue
        k = (b["event_id"], b["market"], b["pick"], b["venue"])
        if k in out and b.get("tier") == "track":
            continue  # prefer the real paper bet's record
        out[k] = {"seg": seg_key(b["venue"], b["sport"], b["market"], b.get("src"), b.get("sport_key")),
                  "ev": b["ev"], "clv": b["closing_fair_p"] / b["cost"] - 1, "event_id": b["event_id"]}
    return out


def from_observations(now, path=OBS_PATH):
    """First sighting (EV >= 0) of each pick per venue, labelled with the pick's last pre-game fair."""
    if not path.exists():
        return {}
    first, close = {}, {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                if _t(r["start"]) > now:
                    continue  # game not started: no closing line yet
                ev, fair, mins = float(r["ev"]), float(r["fair_p"]), float(r["mins_to_start"])
            except (KeyError, ValueError):
                continue
            pk = (r["event_id"], r["market"], r["pick"])
            c = close.get(pk)
            if mins >= 0 and (c is None or mins < c[0]):
                close[pk] = (mins, fair, r["seen_at"])
            k = pk + (r["venue"],)
            if ev >= 0 and k not in first:
                first[k] = (r, ev, fair, mins, r["seen_at"])
    out = {}
    for k, (r, ev, fair, mins, seen) in first.items():
        c = close.get(k[:3])
        if not c or c[0] > MAX_CLOSE_MIN or c[2] <= seen:
            continue  # need a later, near-kickoff fair to call it a closing line
        cost = fair / (1 + ev)
        out[k] = {"seg": seg_key(r["venue"], r["sport"], r["market"]), "ev": ev,
                  "clv": c[1] / cost - 1, "event_id": k[0]}
    return out


# ---------- fit ----------

def fit_segment(rows):
    """Shrunk least squares for CLV = a + b*EV. Returns dict with a, b, n, means, and predicted CLV."""
    n = len(rows)
    xs = [r["ev"] for r in rows]
    ys = [r["clv"] for r in rows]
    mx2 = (sum(x * x for x in xs) / n) if n else 0.03 ** 2
    mx2 = max(mx2, 0.02 ** 2)
    la, lb = PRIOR_N, PRIOR_N * mx2
    # normal equations with priors a~0, b~PRIOR_B
    s1, sx, sxx = n + la, sum(xs), sum(x * x for x in xs) + lb
    sy, sxy = sum(ys), sum(x * y for x, y in zip(xs, ys)) + lb * PRIOR_B
    det = s1 * sxx - sx * sx
    a = (sxx * sy - sx * sxy) / det
    b = (s1 * sxy - sx * sy) / det
    resid = [y - a - b * x for x, y in zip(xs, ys)]
    sd = math.sqrt(sum(e * e for e in resid) / max(n - 2, 1)) if n > 2 else None
    events = len({r["event_id"] for r in rows})
    return {"n": n, "events": events, "a": a, "b": b,
            "mean_ev": sum(xs) / n if n else None, "mean_clv": sum(ys) / n if n else None,
            "beat_close": sum(y > 0 for y in ys) / n if n else None, "resid_sd": sd}


def rule_for(seg, f):
    """Turn a fit into a rule. Not enough data = no rule (default bars apply), except model-priced
    rows, which must earn their way in."""
    src = seg.rsplit("|", 1)[-1]
    if f["n"] < MIN_N_RULE:
        if src == "model":
            return {"blocked": True, "reason": f"model-priced: tracking only until {MIN_N_RULE} graded picks"}
        return {"blocked": False, "bar": None, "reason": f"learning ({f['n']}/{MIN_N_RULE} picks)"}
    if f["b"] < BLOCK_B:
        return {"blocked": True, "reason": f"edge does not survive to the close (retention {f['b']:.0%})"}
    bar = (TARGET_CLV - f["a"]) / f["b"]
    bar = min(max(bar, MIN_BAR), MAX_BAR)
    if bar >= MAX_BAR:
        return {"blocked": True, "reason": "needed EV bar above 10%"}
    return {"blocked": False, "bar": round(bar, 4),
            "reason": f"retention {f['b']:.0%}, drift {f['a']:+.2%}: bar {bar:.1%} for {TARGET_CLV:.0%} expected CLV"}


def run(state=None, now=None, write=True):
    now = now or datetime.now(timezone.utc)
    if state is None:
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    data = from_observations(now)
    data.update(from_state(state))   # state records (exact paid price incl. live) win over observations
    by_seg = {}
    for r in data.values():
        by_seg.setdefault(r["seg"], []).append(r)
    segs = {}
    for seg, rows in sorted(by_seg.items()):
        f = fit_segment(rows)
        segs[seg] = {**{k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()}, **rule_for(seg, f)}
    model_seg = {k: v for k, v in segs.items() if k.endswith("|model")}
    policy = {"generated_at": now.isoformat(), "picks": len(data), "target_clv": TARGET_CLV,
              "model_live": any(not v["blocked"] for v in model_seg.values()), "segments": segs}
    if write:
        POLICY_PATH.write_text(json.dumps(policy, indent=1))
    return policy


# ---------- used by the engine ----------

_cache = {"mtime": None, "policy": None}


def load_policy():
    try:
        m = POLICY_PATH.stat().st_mtime
    except FileNotFoundError:
        return None
    if _cache["mtime"] != m:
        _cache["mtime"], _cache["policy"] = m, json.loads(POLICY_PATH.read_text())
    return _cache["policy"]


def gate(h):
    """None if the hit may be bet, else a reason string. Unknown segments: model rows blocked, others pass."""
    p = load_policy()
    k = seg_key(h["venue"], h.get("sport"), h["market"], h.get("src"), h.get("sport_key"))
    rule = (p or {}).get("segments", {}).get(k)
    if rule is None:
        return "model-priced: tracking only (no data yet)" if h.get("src") == "model" else None
    if rule.get("blocked"):
        return rule["reason"]
    if rule.get("bar") is not None and h["ev"] < rule["bar"]:
        return f"below learned bar {rule['bar']:.1%}"
    return None


def report(policy):
    lines = [f"Learned policy ({policy['picks']} graded picks, target {policy['target_clv']:.0%} expected CLV)"]
    for seg, s in sorted(policy["segments"].items(), key=lambda kv: -kv[1]["n"]):
        state = "BLOCK" if s["blocked"] else (f"bar {s['bar']:.1%}" if s.get("bar") is not None else "default")
        lines.append(f"  {seg:45s} n={s['n']:4d} ev {s['mean_ev'] or 0:+.1%} clv {s['mean_clv'] or 0:+.1%} "
                     f"beat {s['beat_close'] or 0:.0%}  -> {state}: {s['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(report(run(write="--dry" not in sys.argv)))
