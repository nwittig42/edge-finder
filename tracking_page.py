"""/tracking page: the paper tracking list (every live price with positive EV after fees, $10 flat).
Answers: how often do edges show up, and which EV levels actually beat the closing line?"""

from collections import defaultdict
from datetime import datetime, timezone

import ui
from edge_finder import MARKET_LABEL, VENUE_LABEL, parse_time, to_american

BUCKETS = [(0.0, 0.005, "0 to 0.5%"), (0.005, 0.01, "0.5 to 1%"), (0.01, 0.015, "1 to 1.5%"),
           (0.015, 0.03, "1.5 to 3%"), (0.03, 1, "3%+")]


def _clv(t):
    return t["closing_fair_p"] / t["cost"] - 1


def _p(x):
    return f"{x:+.1%}"


def _tone(x):
    return "pos" if x > 0 else "neg" if x < 0 else ""


def _when(iso, now):
    m = (parse_time(iso) - now).total_seconds() / 60
    if m < 0:
        return "started"
    return f"{m:.0f}m" if m < 90 else f"{m / 60:.1f}h"


def render(state):
    now = datetime.now(timezone.utc)
    tr = state.get("tracked", [])
    graded = [t for t in tr if t["status"] in ("won", "lost", "push")]
    closed = [t for t in tr if t.get("closing_at") or t["status"] != "open"]
    open_ = sorted([t for t in tr if t["status"] == "open"], key=lambda t: t["start"])
    day_ago = [t for t in tr if (now - parse_time(t["placed_at"])).total_seconds() < 86400]
    hours = max((now - min((parse_time(t["placed_at"]) for t in tr), default=now)).total_seconds() / 3600, 1)
    pnl = sum(t["pnl"] for t in graded)
    staked = sum(t["stake"] for t in graded)
    clvs = [_clv(t) for t in closed]

    head = ui.stats([
        ("Tracked picks", f"{len(tr)}", f"{len(day_ago)} in the last 24h · ~{len(tr) / hours * 24:.0f}/day"),
        ("Cleared the bar", f"{sum(t['cleared_bar'] for t in tr)}", "would have been real paper bets"),
        ("Beat the close", f"{sum(c > 0 for c in clvs)}/{len(clvs)}" if clvs else "—",
         f"avg CLV {_p(sum(clvs) / len(clvs))}" if clvs else "needs a pre-game odds check"),
        ("Flat-stake P&L", f'<span class="{_tone(pnl)}">{pnl:+.2f}</span>' if graded else "—",
         f"ROI {pnl / staked:+.1%} on {len(graded)} graded" if staked else "graded after games finish"),
    ])

    # What EV level is actually worth betting?
    rows = []
    for lo, hi, label in BUCKETS:
        b = [t for t in tr if lo <= t["ev"] < hi]
        bc = [_clv(t) for t in b if t.get("closing_at") or t["status"] != "open"]
        bg = [t for t in b if t["status"] in ("won", "lost", "push")]
        st = sum(t["stake"] for t in bg)
        rows.append(f'<tr><td>{label}</td><td class="r">{len(b)}</td>'
                    f'<td class="r {_tone(sum(bc))}">{_p(sum(bc) / len(bc)) if bc else "—"}</td>'
                    f'<td class="r">{f"{sum(c > 0 for c in bc) / len(bc):.0%}" if bc else "—"}</td>'
                    f'<td class="r">{len(bg)}</td>'
                    f'<td class="r {_tone(sum(t["pnl"] for t in bg))}">'
                    f'{f"{sum(t["pnl"] for t in bg) / st:+.1%}" if st else "—"}</td></tr>')
    buckets = ('<div class="panel"><h2>Which EV level is worth betting?</h2><p class="lede">The answer this page '
               'exists for. If low-EV picks beat the close too, the bar can come down and you get more bets.</p>'
               '<table><tr><th>EV when tracked</th><th class="r">Picks</th><th class="r">Avg CLV</th>'
               '<th class="r">Beat close</th><th class="r">Graded</th><th class="r">ROI</th></tr>'
               + "".join(rows) + "</table></div>")

    # By venue and market
    seg = defaultdict(list)
    for t in tr:
        seg[(VENUE_LABEL.get(t["venue"], t["venue"]), MARKET_LABEL[t["market"]])].append(t)
    srows = "".join(
        f'<tr><td>{v}</td><td>{m}</td><td class="r">{len(ts)}</td><td class="r">{_p(sum(t["ev"] for t in ts) / len(ts))}</td>'
        f'<td class="r">{(lambda c: _p(sum(c) / len(c)) if c else "—")([_clv(t) for t in ts if t.get("closing_at")])}</td></tr>'
        for (v, m), ts in sorted(seg.items(), key=lambda kv: -len(kv[1])))
    segs = ('<div class="panel"><h2>Where they come from</h2><table><tr><th>Venue</th><th>Market</th>'
            '<th class="r">Picks</th><th class="r">Avg EV</th><th class="r">Avg CLV</th></tr>' + srows + "</table></div>") if tr else ""

    def row(t):
        c = _clv(t)
        res = t["status"] if t["status"] != "open" else _when(t["start"], now)
        return (f'<tr><td>{ui.esc(t["pick"])} <span class="muted">{MARKET_LABEL[t["market"]]}</span><br>'
                f'<span class="muted">{ui.esc(t["event"])} · {ui.esc(t["sport"])}</span></td>'
                f'<td>{VENUE_LABEL.get(t["venue"], t["venue"])}</td>'
                f'<td class="r">{round(100 / t["price"])}¢ <span class="muted">({to_american(t["price"])})</span></td>'
                f'<td class="r">{round(100 * t["fair_p"], 1)}¢</td><td class="r">{ui.ev_pill(t["ev"], t["bar"])}</td>'
                f'<td class="r {_tone(c)}">{_p(c)}</td><td class="r">{t["n_books"]}</td><td class="r">{res}</td>'
                + (f'<td class="r {_tone(t["pnl"])}">{t["pnl"]:+.2f}</td>' if t["pnl"] is not None else '<td class="r">—</td>')
                + "</tr>")

    th = ('<table><tr><th>Pick</th><th>Venue</th><th class="r">Price</th><th class="r">Fair</th><th class="r">EV</th>'
          '<th class="r">CLV</th><th class="r">Sharps</th><th class="r">Starts / result</th><th class="r">P&amp;L</th></tr>')
    open_html = th + "".join(row(t) for t in open_) + "</table>" if open_ else \
        '<p class="lede">Nothing open. Picks appear here the moment a live price has positive EV after fees.</p>'
    done = sorted(graded, key=lambda t: t.get("settled_at", ""), reverse=True)[:60]
    done_html = th + "".join(row(t) for t in done) + "</table>" if done else '<p class="lede">No graded picks yet.</p>'

    body = ('<h1>Paper tracking</h1><p class="lede">Every live Kalshi / Polymarket US price with positive EV after fees, '
            'tracked as a $10 pretend bet. No texts, separate from the paper bankroll. EV pill turns green when it '
            'cleared the real betting bar.</p>' + head + buckets +
            ui.tabs("track", [("t-open", "Open", len(open_), open_html), ("t-done", "Graded", len(graded), done_html)])
            + segs)
    return body
