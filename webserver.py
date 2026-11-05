"""Local monitoring website, served by the engine itself at http://localhost:8787
Only listens on this Mac (127.0.0.1). Pages: +EV bets, odds screen, go-live check, engine log, plan, JSON."""

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor
import tracking_page
import ui
from edge_finder import HERE, MARKET_LABEL, VENUE_LABEL

PORT = 8787


def md_to_html(md):
    """Small markdown renderer: headings, tables, lists, checkboxes, code blocks, bold."""
    out, in_code, in_table, in_list = [], False, False, False

    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    for line in md.splitlines():
        if line.startswith("```"):
            out.append("</pre></div>" if in_code else '<div class="panel"><pre>')
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if in_table and not line.startswith("|"):
            out.append("</table></div>")
            in_table = False
        if in_list and not line.lstrip().startswith("- "):
            out.append("</ul>")
            in_list = False
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                out.append('<div class="panel scroll"><table>')
                in_table = True
                out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
        elif line.startswith("#"):
            n = min(len(line) - len(line.lstrip("#")), 4)
            out.append(f"<h{n}>{inline(line.lstrip('# '))}</h{n}>")
        elif line.lstrip().startswith("- "):
            if not in_list:
                out.append('<ul style="padding-left:20px">')
                in_list = True
            item = line.lstrip()[2:].replace("[x]", "✅").replace("[ ]", "⬜")
            out.append(f"<li>{inline(item)}</li>")
        elif line.strip() == "---":
            out.append('<hr style="border:0;border-top:1px solid var(--line);margin:20px 0">')
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_table:
        out.append("</table></div>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _cfg():
    return json.loads((HERE / "config.json").read_text())


def _p(x, sign=True):
    return "n/a" if x is None else (f"{x:+.1%}" if sign else f"{x:.0%}")


def _tone(x):
    return "" if x is None else "pos" if x > 0 else "neg" if x < 0 else "muted"


def _table(head, rows, right_from=1):
    th = "".join(f'<th class="{"r" if i >= right_from else ""}">{h}</th>' for i, h in enumerate(head))
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _clv_rows(groups):
    return [f'<tr><td>{html.escape(str(VENUE_LABEL.get(k) or MARKET_LABEL.get(k) or k))}</td><td class="r">{v["n"]}</td>'
            f'<td class="r {_tone(v["avg"])}"><b>{_p(v["avg"])}</b></td><td class="r">{_p(v["median"])}</td>'
            f'<td class="r">{_p(v["beat"], False)}</td></tr>' for k, v in groups.items() if v["n"]]


def render_monitor(m):
    h, v, c, pl = m["health"], m["volume"], m["clv"], m["pnl"]
    tag = {"PASS": "pos", "FAIL": "neg", "NOT YET": ""}
    passed = sum(x["status"] == "PASS" for x in m["checklist"])

    head, _, rest = m["verdict"].partition(":")
    hero = (f'<div class="panel" style="padding:20px 22px"><div style="display:flex;gap:12px;align-items:center;'
            f'flex-wrap:wrap"><span class="tag {m["tone"]}" style="font-size:13px;padding:4px 12px">{html.escape(head)}'
            f'</span><span style="font-size:16px;font-weight:600">{html.escape(rest.strip())}</span></div>'
            f'<div style="margin-top:16px;display:flex;justify-content:space-between" class="sub"><span>Settled bets '
            f'with a valid close</span><span>{v["valid"]} of 150</span></div>'
            f'<div style="height:8px;border-radius:4px;background:var(--chip);margin-top:6px;overflow:hidden">'
            f'<div style="height:100%;width:{min(v["valid"] / 150, 1):.0%};background:var(--brand)"></div></div></div>')
    if m["low_volume"]:
        hero += ('<div class="panel" style="padding:14px 18px"><span class="tag warn">Low volume</span> Under 5 bets a '
                 'day after 4 days. Options: add sports or markets, props, or lower the EV bar (never below fees + 1%). '
                 'Nothing changes until you say so.</div>')

    eta = f'{v["eta_days"]:.0f} days' if v["eta_days"] else "n/a"
    age = h["cycle_age_min"]
    burn = f'{h["burn_per_day"]:,.0f} a day' if h["burn_per_day"] else "measuring"
    top = ui.stats([
        ("Avg CLV", f'<span class="{_tone(c["all"]["avg"])}">{_p(c["all"]["avg"])}</span>', "target +1.5% or more"),
        ("Beat the close", _p(c["all"]["beat"], False), "target over 55%"),
        ("Bets per day", f'{v["placed_per_day"]:.1f}', f"150 settled in {eta}"),
        ("Checks passed", f"{passed} of {len(m['checklist'])}", "all six to go live"),
    ])

    checks = "".join(f'<tr><td style="width:96px"><span class="tag {tag.get(x["status"], "")}">'
                     f'{x["status"].capitalize()}</span></td><td>{html.escape(x["item"])}'
                     f'<div class="sub">{html.escape(x["detail"])}</div></td></tr>' for x in m["checklist"])
    check_panel = (f'<div class="panel"><div class="ph"><h2>Go-live checklist</h2><span class="hint">Live money '
                   f'only when all six pass</span></div><table><tbody>{checks}</tbody></table></div>')

    # Detail tabs
    clv_rows = _clv_rows({"All bets": c["all"], "Last 7 days": c["last7"]})
    clv_html = (_table(["Group", "Bets", "Avg CLV", "Median", "Beat close"],
                       clv_rows + [f'<tr><td colspan="5" class="sub" style="background:var(--bg)">{t}</td></tr>'
                                   + "".join(_clv_rows(g)) for t, g in
                                   (("By venue", c["by_venue"]), ("By market", c["by_market"]),
                                    ("By sport", c["by_sport"])) if _clv_rows(g)])
                if clv_rows else '<div class="empty">CLV shows here once the first bets settle.</div>')
    if c["excluded"]:
        clv_html += ('<div class="ph" style="border-top:1px solid var(--line)"><h2>Left out of the verdict</h2>'
                     '<span class="hint">Close missing or taken over an hour before start, so CLV looks better than '
                     'it is</span></div>' + _table(["Pick", "Close taken", "CLV"], [
                         f'<tr><td>{html.escape(b["pick"])}<div class="sub">{html.escape(b["event"])}</div></td>'
                         f'<td class="r sub">{html.escape(str(b["closing_at"] or "missing"))}</td>'
                         f'<td class="r">{_p(b["clv"])}</td></tr>' for b in c["excluded"]]))

    ev, tot = m["ev"], sum(m["ev"]["buckets"].values()) or 1
    ev_rows = [f'<tr><td class="nw">{k} EV</td><td style="width:65%"><div style="height:8px;border-radius:4px;'
               f'background:var(--chip)"><div style="height:100%;border-radius:4px;width:{n / tot:.0%};background:'
               f'{"var(--warn)" if k == "8%+" else "var(--brand)"}"></div></div></td><td class="r">{n}</td></tr>'
               for k, n in ev["buckets"].items()]
    flag = ('<div class="ph"><span class="tag warn">Check prices</span><span class="hint">Over 25% of bets show 8%+ EV. '
            'Real edges are rarely that big, so prices may be stale or mismatched.</span></div>'
            if ev["high_share"] and ev["high_share"] > 0.25 else "")
    conc_rows = [f'<tr><td>{f.title()}</td><td>{html.escape(str(VENUE_LABEL.get(x["top"]) or MARKET_LABEL.get(x["top"]) or x["top"]))}</td><td class="r '
                 f'{"warn" if x["share"] > .6 else ""}"><b>{x["share"]:.0%}</b></td></tr>'
                 for f, x in m["concentration"].items()]
    edge_html = (flag + _table(["Edge at bet time", "", "Bets"], ev_rows, 2)
                 + '<div class="ph" style="border-top:1px solid var(--line)"><h2>Where the edge comes from</h2>'
                 '<span class="hint">Share of positive CLV. Over 60% from one place is flagged.</span></div>'
                 + (_table(["Split", "Biggest", "Share"], conc_rows, 2) if conc_rows
                    else '<div class="empty">Shows once bets settle.</div>'))

    pnl_html = _table(["", "Value"], [
        f'<tr><td>Actual profit</td><td class="r {_tone(pl["actual"])}"><b>${pl["actual"]:+,.2f}</b></td></tr>',
        f'<tr><td>Expected from the edges</td><td class="r">${pl["expected"]:+,.2f}</td></tr>',
        f'<tr><td>Normal swing (1 SD)</td><td class="r">${pl["sd"]:,.2f}</td></tr>',
        f'<tr><td>Gap in SDs</td><td class="r">{f"{pl["z"]:+.2f}" if pl["z"] is not None else "n/a"}'
        f' <span class="sub">(within ±2 is just luck)</span></td></tr>'])

    health_html = _table(["", "Status"], [
        f'<tr><td>Engine</td><td class="r">{"<span class=pos>Running</span>" if age is not None and age < 10 else "<span class=neg>Stalled</span>"}'
        f' <span class="sub">last check {f"{age:.0f} min ago" if age is not None else "never"}</span></td></tr>',
        f'<tr><td>Errors, last 24 hours</td><td class="r {"neg" if h["n_errors_24h"] else "pos"}">{h["n_errors_24h"]}</td></tr>',
        f'<tr><td>API credits left</td><td class="r">{h["credits"] or 0:,}</td></tr>',
        f'<tr><td>Credit use</td><td class="r">{burn}'
        + (f' <span class="{"pos" if h["runout_days"] >= h["days_left_month"] else "neg"}">lasts '
           f'{h["runout_days"]:.0f} days, month ends in {h["days_left_month"]:.0f}</span>' if h["runout_days"] else "")
        + '</td></tr>',
        f'<tr><td>Days running</td><td class="r">{h["days_running"]:.1f}</td></tr>',
        f'<tr><td>Bets</td><td class="r">{v["placed"]} placed, {v["open"]} open, {v["settled"]} settled</td></tr>'])
    if h["errors_24h"]:
        health_html += "<pre>" + html.escape("\n".join(h["errors_24h"])) + "</pre>"

    hist = monitor.history()
    hist_html = (f'<div class="prose">{md_to_html(hist)}</div>' if hist.strip() else
                 '<div class="empty">No check-ins yet. The Claude monitor adds one here each time it runs.</div>')

    return (f'<h1>Go-live check</h1><p class="lede">Is the edge real, and is it ready for real money? '
            f'Updated every minute from the engine\'s own records.</p>{hero}{top}{check_panel}'
            + ui.tabs("golive", [("g-clv", "Closing line value", None, clv_html),
                                 ("g-edge", "Edge size", None, edge_html),
                                 ("g-pnl", "Profit vs expected", None, pnl_html),
                                 ("g-health", "Health", h["n_errors_24h"] or None, health_html),
                                 ("g-hist", "Check-in history", None, hist_html)]))


class Handler(BaseHTTPRequestHandler):
    state_ref = None  # callable returning the engine's STATE

    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def page(self, title, active, body, refresh=60, code=200):
        self.send(ui.shell(title, active, body, Handler.state_ref() or {}, refresh), code=code)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
        elif path in ("/dashboard.html", "/odds.html"):
            f = HERE / path.lstrip("/")
            if f.exists():
                self.send(f.read_bytes())
            else:
                self.page("Starting", "bets", "<h1>Starting up</h1><p class='lede'>Building pages. "
                          "This refreshes in 10 seconds.</p>", 10)
        elif path in ("/static/logo.png", "/static/favicon.png"):
            self.send((HERE / path.lstrip("/")).read_bytes(), "image/png")
        elif path == "/favicon.ico":
            self.send((HERE / "static" / "favicon.png").read_bytes(), "image/png")
        elif path == "/log":
            lines = (HERE / "logs" / "engine.log").read_text().splitlines()[-250:]
            body = ('<h1>Engine log</h1><p class="lede">Last 250 lines, newest at the bottom. Refreshes every 15 '
                    'seconds.</p><div class="panel"><pre>' + html.escape("\n".join(lines)) + "</pre></div>")
            self.page("Edge Finder log", "log", body, 15)
        elif path == "/plan":
            body = f'<div class="panel prose">{md_to_html((HERE / "PLAN.md").read_text())}</div>'
            self.page("Edge Finder plan", "plan", body, None)
        elif path == "/monitor":
            self.page("Edge Finder go-live", "golive",
                      render_monitor(monitor.compute(Handler.state_ref() or {}, _cfg())))
        elif path == "/api/monitor":
            self.send(json.dumps(monitor.compute(Handler.state_ref() or {}, _cfg()), indent=1, default=str),
                      "application/json")
        elif path == "/tracking":
            self.page("Edge Finder tracking", "track", tracking_page.render(Handler.state_ref() or {}))
        elif path == "/api/tracking":
            self.send(json.dumps((Handler.state_ref() or {}).get("tracked", []), indent=1, default=str),
                      "application/json")
        elif path == "/api/state":
            s = Handler.state_ref() or {}
            keep = {k: s.get(k) for k in ("balances", "credits_remaining", "last_cycle", "last_cycle_fetch",
                                          "interval_min", "urgent_used")}
            keep["open_bets"] = [b for b in s.get("bets", []) if b["status"] == "open"]
            keep["settled_bets"] = [b for b in s.get("bets", []) if b["status"] != "open"][-50:]
            keep["news"] = s.get("news", [])[-20:]
            self.send(json.dumps(keep, indent=1, default=str), "application/json")
        else:
            self.page("Not found", "", "<h1>Page not found</h1><p class='lede'>Pick a page from the menu.</p>",
                      None, code=404)

    def log_message(self, *a):
        pass  # keep the engine log clean


def start(state_ref, log):
    Handler.state_ref = state_ref
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"website not started (port {PORT}: {e})")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"website at http://localhost:{PORT}")
