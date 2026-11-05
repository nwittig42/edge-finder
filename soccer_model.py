"""Dixon-Coles soccer score model, fitted to sharp prices (no history needed).

Idea: pick each team's expected goals (and the low-score correction rho) so the model reproduces the
sharp no-vig 1X2 and .5-line totals/handicaps. The fitted score grid then prices ANY outcome or
combination of outcomes on the same game, including correlated parlay legs that books price as if
independent.

Reference: Dixon & Coles (1997), "Modelling Association Football Scores and Inefficiencies in the
Football Betting Market", Applied Statistics 46(2). Stdlib only (the engine's Python has no numpy).
"""

import math

MAX_GOALS = 10
RHO_PRIOR = -0.06      # typical fitted values sit around -0.03 to -0.13
RHO_BOUNDS = (-0.25, 0.10)
RHO_PRIOR_WEIGHT = 0.02


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_grid(lh, la, rho):
    """P[h][a] for h, a in 0..MAX_GOALS, with the Dixon-Coles low-score adjustment."""
    ph = [_pois(k, lh) for k in range(MAX_GOALS + 1)]
    pa = [_pois(k, la) for k in range(MAX_GOALS + 1)]
    g = [[ph[h] * pa[a] for a in range(MAX_GOALS + 1)] for h in range(MAX_GOALS + 1)]
    g[0][0] *= 1 - lh * la * rho
    g[0][1] *= 1 + lh * rho
    g[1][0] *= 1 + la * rho
    g[1][1] *= 1 - rho
    s = sum(map(sum, g))
    return [[x / s for x in row] for row in g]


# ---------- legs ----------
# A leg is a tuple. Supported:
#   ("result", "home" | "draw" | "away")
#   ("total", "over" | "under", line)            line must be x.5
#   ("handicap", "home" | "away", point)         team + point beats the other; point must be x.5
#   ("btts", "yes" | "no")
#   ("team_total", "home" | "away", "over" | "under", line)

def leg_ok(leg, h, a):
    kind = leg[0]
    if kind == "result":
        return {"home": h > a, "draw": h == a, "away": a > h}[leg[1]]
    if kind == "total":
        return (h + a > leg[2]) if leg[1] == "over" else (h + a < leg[2])
    if kind == "handicap":
        mine, theirs = (h, a) if leg[1] == "home" else (a, h)
        return mine + leg[2] > theirs
    if kind == "btts":
        return (h > 0 and a > 0) == (leg[1] == "yes")
    if kind == "team_total":
        g = h if leg[1] == "home" else a
        return (g > leg[3]) if leg[2] == "over" else (g < leg[3])
    raise ValueError(f"unknown leg {leg!r}")


def prob(grid, legs):
    """Probability that every leg wins (a same-game parlay), straight from the score grid."""
    return sum(grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1)
               if all(leg_ok(l, h, a) for l in legs))


# ---------- fitting ----------

def _nelder_mead(f, x0, step=0.2, iters=400, tol=1e-10):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if vals[-1] - vals[0] < tol:
            break
        c = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        xr = [c[j] + (c[j] - pts[-1][j]) for j in range(n)]
        fr = f(xr)
        if fr < vals[0]:
            xe = [c[j] + 2 * (c[j] - pts[-1][j]) for j in range(n)]
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = [c[j] + 0.5 * (pts[-1][j] - c[j]) for j in range(n)]
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = min(range(n + 1), key=lambda i: vals[i])
    return pts[i], vals[i]


def fit(targets):
    """targets: list of (legs, fair_prob). Needs at least the 1X2 to be meaningful.
    Returns {"lh", "la", "rho", "rmse", "n"} or None."""
    if len(targets) < 2:
        return None
    lo, hi = RHO_BOUNDS

    def unpack(x):
        return math.exp(x[0]), math.exp(x[1]), lo + (hi - lo) / (1 + math.exp(-x[2]))

    def loss(x):
        lh, la, rho = unpack(x)
        if not (0.05 < lh < 6 and 0.05 < la < 6):
            return 1e9
        g = score_grid(lh, la, rho)
        err = sum((prob(g, legs) - p) ** 2 for legs, p in targets)
        return err + RHO_PRIOR_WEIGHT * (rho - RHO_PRIOR) ** 2

    r0 = math.log((RHO_PRIOR - lo) / (hi - RHO_PRIOR))
    x, _ = _nelder_mead(loss, [math.log(1.4), math.log(1.1), r0])
    lh, la, rho = unpack(x)
    g = score_grid(lh, la, rho)
    rmse = math.sqrt(sum((prob(g, legs) - p) ** 2 for legs, p in targets) / len(targets))
    return {"lh": lh, "la": la, "rho": rho, "rmse": rmse, "n": len(targets), "grid": g}


# ---------- glue to the engine's odds formats ----------

def _half(x):
    return x is not None and abs(x * 2 - round(x * 2)) < 1e-9 and abs(x - round(x)) > 1e-9


def leg_for(market, name, point, home, away):
    """Map an Odds API outcome to a model leg, or None if the model can't price it."""
    if market == "h2h":
        return ("result", "home" if name == home else "away" if name == away else "draw")
    if market == "totals" and _half(point):
        return ("total", name.lower(), point)
    if market == "spreads" and _half(point) and name in (home, away):
        return ("handicap", "home" if name == home else "away", point)
    return None


def targets_from_board(ev):
    """Fit targets from a board event (engine.board_event): every row with a sharp fair the model can express."""
    out = []
    for mk, m in ev.get("markets", {}).items():
        for r in m["rows"]:
            if r.get("fair") and r.get("src") != "model":
                leg = leg_for(mk, r["name"], r.get("point"), ev["home"], ev["away"])
                if leg:
                    out.append(((leg,), r["fair"]))
    return out


def targets_from_fairs(fairs, home, away):
    """fairs: {market: {(name, point): fair_p}} -> fit targets."""
    out = []
    for mk, d in fairs.items():
        for (name, point), p in d.items():
            leg = leg_for(mk, name, point, home, away)
            if leg and p:
                out.append(((leg,), p))
    return out


def good_fit(m, max_rmse=0.012):
    """Only trust a fit that has the 3-way result plus at least one goals line, and reproduces them closely."""
    if not m:
        return False
    kinds = {legs[0][0] for legs, _ in m.get("_targets", [])}
    return m["rmse"] <= max_rmse and "result" in kinds and kinds & {"total", "handicap"}


def fit_checked(targets, max_rmse=0.012):
    m = fit(targets)
    if m:
        m["_targets"] = targets
    return m if good_fit(m, max_rmse) else None


def fill_board_event(ev):
    """Give a fair price to board rows the sharp books don't quote at that exact line (e.g. Kalshi's
    'wins by 2+' when Pinnacle only hangs -0.25), priced from the score model fitted to the sharp rows.
    Model rows are marked src="model"; the learner keeps them tracking-only until they prove out.
    Returns the fit (or None)."""
    m = fit_checked(targets_from_board(ev))
    for mk, mkt in ev.get("markets", {}).items():
        for r in mkt["rows"]:
            if r.get("fair") and r.get("src") != "model":
                continue
            leg = leg_for(mk, r["name"], r.get("point"), ev["home"], ev["away"])
            if m and leg:
                r.update(fair=round(prob(m["grid"], (leg,)), 4), n=0, spread=None, src="model")
            elif r.get("src") == "model":
                r.update(fair=None, src=None)
    if m:
        ev["model"] = {"lh": round(m["lh"], 3), "la": round(m["la"], 3), "rho": round(m["rho"], 3),
                       "rmse": round(m["rmse"], 4)}
    return m


if __name__ == "__main__":
    # Smoke test: a typical EPL favourite. Pinnacle-ish no-vig: H .55 D .25 A .20, over 2.5 .52
    t = [((("result", "home"),), .55), ((("result", "draw"),), .25), ((("result", "away"),), .20),
         ((("total", "over", 2.5),), .52), ((("total", "under", 2.5),), .48)]
    m = fit_checked(t)
    g = m["grid"]
    print(f"lh={m['lh']:.3f} la={m['la']:.3f} rho={m['rho']:+.3f} rmse={m['rmse']:.4f}")
    for legs in [(("result", "home"), ("total", "over", 2.5)), (("result", "home"), ("btts", "no")),
                 (("result", "draw"), ("total", "under", 2.5)), (("handicap", "home", -1.5),)]:
        joint = prob(g, legs)
        naive = math.prod(prob(g, (l,)) for l in legs)
        print(f"{legs}: joint {joint:.3f}  naive {naive:.3f}  lift {joint / naive:.2f}")
