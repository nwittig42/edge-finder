"""Renders ~/edge-finder/dashboard.html from the engine's state after every cycle.
Open it in a browser; it reloads itself every 60 seconds."""

import html
import json
from collections import defaultdict
from datetime import datetime, timezone

import ui
from edge_finder import HERE, book_list, MARKET_LABEL, VENUE_LABEL, parse_time, to_american

OUT = HERE / "dashboard.html"

CSS = ui.CSS


esc = ui.esc


def money(x, sign=False):
    return f"{'+' if sign and x > 0 else '-' if x < 0 else ''}${abs(x):,.2f}"


def pct(x, sign=True):
    return f"{x:+.1%}" if sign else f"{x:.1%}"


def cls(x):
    return "pos" if x > 0 else "neg" if x < 0 else "muted"


def ago(iso, now):
    if not iso:
        return "never"
    m = (now - parse_time(iso)).total_seconds() / 60
    return "just now" if m < 1 else f"{m:.0f} min ago" if m < 90 else f"{m / 60:.1f} h ago"


def until(iso, now):
    m = (parse_time(iso) - now).total_seconds() / 60
    if m < 0:
        return "started"
    return f"{m:.0f}m" if m < 90 else f"{m / 60:.1f}h"


def pnl_chart(settled):
    if not settled:
        return '<div class="empty">The profit chart starts once the first paper bet is graded.</div>'
    pts, total = [0.0], 0.0
    for b in settled:
        total += b["pnl"]
        pts.append(total)
    w, h, pad = 640, 180, 28
    lo, hi = min(pts + [0]), max(pts + [0])
    span = (hi - lo) or 1
    xs = [pad + i * (w - 2 * pad) / max(len(pts) - 1, 1) for i in range(len(pts))]
    ys = [h - pad - (p - lo) * (h - 2 * pad) / span for p in pts]
    zero = h - pad - (0 - lo) * (h - 2 * pad) / span
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    color = "var(--pos)" if total >= 0 else "var(--neg)"
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Cumulative profit">'
            f'<line x1="{pad}" x2="{w - pad}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="var(--line)"/>'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.5" fill="{color}"/>'
            f'<text x="{w - pad}" y="{max(ys[-1] - 8, 12):.1f}" text-anchor="end">{esc(money(total, True))}</text>'
            f'<text x="{pad}" y="{h - 8}">bet 1</text><text x="{w - pad}" y="{h - 8}" text-anchor="end">'
            f'bet {len(pts) - 1}</text></svg>')


def render(state, cfg):
    now = datetime.now(timezone.utc)
    bets = state.get("bets", [])
    settled = sorted([b for b in bets if b["status"] in ("won", "lost", "push")],
                     key=lambda b: b.get("settled_at", ""))
    open_bets = sorted([b for b in bets if b["status"] == "open"], key=lambda b: b["start"])
    start_bank = sum(cfg["bankroll_by_venue"].values())
    exposure = sum(b["stake"] for b in open_bets)
    balance = sum(state["balances"].values()) + exposure
    pnl = sum(b["pnl"] for b in settled)
    staked = sum(b["stake"] for b in settled)
    w = sum(b["status"] == "won" for b in settled)
    l = sum(b["status"] == "lost" for b in settled)
    p = sum(b["status"] == "push" for b in settled)
    clvs = [b["closing_fair_p"] / b["cost"] - 1 for b in settled]
    beat = sum(c > 0 for c in clvs)

    top = ui.stats([
        ("Bankroll", money(balance), f"started at {money(start_bank)}"),
        ("Profit", f'<span class="{cls(pnl)}">{money(pnl, True)}</span>',
         f"ROI {pct(pnl / staked)}" if staked else "no graded bets yet"),
        ("Beat the closing line", f'<span class="{cls(sum(clvs))}">{pct(sum(clvs) / len(clvs))}</span>' if clvs else "n/a",
         f"{beat} of {len(clvs)} bets" if clvs else "the real test of an edge"),
        ("Record", f"{w}-{l}-{p}", f"{len(settled)} graded"),
        ("Open bets", str(len(open_bets)), f"{money(exposure)} at risk"),
    ])

    # Main table: every venue price vs fair from the last check, best first
    radar_rows, radar_at = [], None
    for sport, r in state.get("radar", {}).items():
        radar_at = max(radar_at or r["at"], r["at"])
        for o in r["rows"]:
            radar_rows.append(o)
    radar_rows = sorted(radar_rows, key=lambda o: o["ev"], reverse=True)[:12]
    if radar_rows:
        body = "".join(
            f'<tr><td>{ui.ev_pill(o["ev"], o["bar"])}</td>'
            f'<td><div class="pick">{esc(o["pick"])}</div><div class="sub">{esc(o["event"])}</div></td>'
            f'<td class="nw">{MARKET_LABEL[o["market"]]}<div class="sub">{esc(o["sport"])}</div></td>'
            f'<td class="nw">{VENUE_LABEL[o["venue"]]}</td>'
            f'<td class="r nw"><b>{round(100 / o["price"])}¢</b></td>'
            f'<td class="r nw muted">{round(100 * o["fair_p"])}¢</td>'
            f'<td class="r nw sub">{until(o["start"], now)}</td></tr>' for o in radar_rows)
        main = ('<div class="scroll"><table><thead><tr><th>EV</th><th>Pick</th><th>Market</th><th>Venue</th>'
                '<th class="r">Price</th><th class="r">Fair</th><th class="r">Starts</th></tr></thead>'
                f'<tbody>{body}</tbody></table></div>')
    else:
        main = ('<div class="empty">Waiting for the next price check. Every Kalshi and Polymarket US price will '
                'show here, best first.</div>')
    main_panel = (f'<div class="panel"><div class="ph"><h2>Best prices right now</h2><span class="hint">'
                  f'Green clears the bar and gets bet. Amber is within 1.5% of it. '
                  f'{"Checked " + ago(radar_at, now) if radar_at else "No price check yet"}.</span></div>{main}</div>')

    # Tabs
    if open_bets:
        rows = "".join(
            f'<tr><td>{ui.ev_pill(b["ev"])}</td>'
            f'<td><div class="pick">{esc(b["pick"])}</div><div class="sub">{esc(b["event"])}</div></td>'
            f'<td class="nw">{VENUE_LABEL[b["venue"]]}</td><td class="r nw">{round(100 / b["price"])}¢</td>'
            f'<td class="r nw {cls(b["closing_fair_p"] / b["cost"] - 1)}">{pct(b["closing_fair_p"] / b["cost"] - 1)}</td>'
            f'<td class="r nw">{money(b["stake"])}</td><td class="r nw sub">{until(b["start"], now)}</td></tr>'
            for b in open_bets)
        open_html = ('<div class="scroll"><table><thead><tr><th>EV</th><th>Pick</th><th>Venue</th><th class="r">Paid</th>'
                     '<th class="r">CLV now</th><th class="r">Stake</th><th class="r">Starts</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
    else:
        open_html = '<div class="empty">No open bets. A bet opens when a price turns green above.</div>'

    if settled:
        rows = "".join(
            f'<tr><td><span class="tag {"pos" if b["status"] == "won" else "neg" if b["status"] == "lost" else ""}">'
            f'{b["status"].title()}</span></td>'
            f'<td><div class="pick">{esc(b["pick"])}</div><div class="sub">{esc(b["event"])}</div></td>'
            f'<td class="nw">{VENUE_LABEL[b["venue"]]}</td>'
            f'<td class="r nw {cls(b["closing_fair_p"] / b["cost"] - 1)}">{pct(b["closing_fair_p"] / b["cost"] - 1)}</td>'
            f'<td class="r nw {cls(b["pnl"])}">{money(b["pnl"], True)}</td></tr>' for b in reversed(settled[-30:]))
        hist_html = (f'<div style="padding:16px 18px 4px">{pnl_chart(settled)}</div>'
                     '<div class="scroll"><table><thead><tr><th>Result</th><th>Pick</th><th>Venue</th>'
                     f'<th class="r">CLV</th><th class="r">Profit</th></tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        hist_html = '<div class="empty">Results and a profit chart show here once bets are graded.</div>'

    seg = defaultdict(lambda: {"n": 0, "stake": 0.0, "pnl": 0.0, "clv": []})
    for b in settled:
        k = (b["sport"], MARKET_LABEL[b["market"]], VENUE_LABEL[b["venue"]])
        seg[k]["n"] += 1
        seg[k]["stake"] += b["stake"]
        seg[k]["pnl"] += b["pnl"]
        seg[k]["clv"].append(b["closing_fair_p"] / b["cost"] - 1)
    if seg:
        rows = "".join(
            f'<tr><td>{esc(k[0])}</td><td>{k[1]}</td><td>{k[2]}</td><td class="r">{v["n"]}</td>'
            f'<td class="r {cls(sum(v["clv"]))}">{pct(sum(v["clv"]) / len(v["clv"]))}</td>'
            f'<td class="r {cls(v["pnl"])}">{money(v["pnl"], True)}</td></tr>'
            for k, v in sorted(seg.items(), key=lambda kv: -kv[1]["n"]))
        seg_html = ('<div class="scroll"><table><thead><tr><th>Sport</th><th>Market</th><th>Venue</th>'
                    '<th class="r">Bets</th><th class="r">Avg CLV</th><th class="r">Profit</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table></div>')
    else:
        seg_html = '<div class="empty">Once bets are graded, this shows which sports and venues have an edge.</div>'

    feed = list(reversed(state.get("news", [])))[:15]
    if feed:
        rows = "".join(
            f'<tr><td><div class="pick">{esc(n["player"])}</div><div class="sub">{esc(n["team"])}</div></td>'
            f'<td class="nw"><span class="muted">{esc(n["old"])}</span> to <b>{esc(n["new"])}</b></td>'
            f'<td class="sub">{esc(n["comment"][:110])}</td><td class="r nw sub">{ago(n["seen_at"], now)}</td></tr>'
            for n in feed)
        news_html = ('<div class="scroll"><table><thead><tr><th>Player</th><th>Status</th><th>Note</th>'
                     f'<th class="r">Seen</th></tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        news_html = '<div class="empty">No injury changes yet. NBA, NFL and NHL feeds are checked every 3 minutes.</div>'

    body = (f'<h1>+EV bets</h1><p class="lede">Kalshi and Polymarket US prices against the sharp fair price, '
            f'after fees.</p>{top}{main_panel}'
            + ui.tabs("home", [("t-open", "Open bets", len(open_bets), open_html),
                               ("t-res", "Results", len(settled), hist_html),
                               ("t-seg", "By sport", None, seg_html),
                               ("t-news", "Injury news", len(feed), news_html)]))
    OUT.write_text(ui.shell("Edge Finder", "bets", body, state))


# ---------- odds screen ----------

ODDS_OUT = HERE / "odds.html"
BOOK_LABEL = {"pinnacle": "Pinnacle", "betfair_ex_eu": "Betfair", "kalshi": "Kalshi", "polymarket_us": "Poly US",
              "draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "williamhill_us": "Caesars",
              "betonlineag": "BetOnline", "novig": "Novig", "prophetx": "ProphetX"}

ODDS_CSS = """
.oddsbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin:0 0 14px}
.oddsbar .chips{align-items:center}
tr.game td{background:var(--bg);padding:8px 14px;font-size:12.5px;color:var(--muted);border-bottom:1px solid var(--line)}
tr.game b{color:var(--ink);font-size:13.5px;margin-right:8px}
td.px{text-align:center;white-space:nowrap;font-weight:500;min-width:64px}
td.px.best{font-weight:700;color:var(--pos)}td.px.venue{background:color-mix(in srgb,var(--accent) 5%,transparent)}
th.venue{color:var(--ink);font-weight:600}
td.px .e{display:block;font-size:11px;font-weight:600}
td.fair{text-align:center;font-weight:650;white-space:nowrap}
"""

ODDS_JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const L = D.labels, V = D.venues, now = new Date(D.now);
const S = (()=>{try{return JSON.parse(localStorage.getItem('ef_odds')||'{}')}catch(e){return {}}})();
const $ = id => document.getElementById(id);
function save(){try{localStorage.setItem('ef_odds',JSON.stringify(S))}catch(e){}}
function am(d){if(!d)return '';return d>=2?'+'+Math.round((d-1)*100):String(Math.round(-100/(d-1)))}
function cost(v,d){const p=1/d;if(v==='kalshi')return p+D.fees.kalshi_taker_coefficient*p*(1-p);
  if(v.startsWith('polymarket'))return p+D.fees.polymarket_us_taker_coefficient*p*(1-p);return p}
function ago(iso){if(!iso)return '';const m=(now-new Date(iso))/60000;return m<1?'just now':Math.round(m)+' min ago'}
function when(iso){return new Date(iso).toLocaleString([], {weekday:'short',hour:'numeric',minute:'2-digit'})}
function pick(mk,r){if(mk==='spreads')return r.name+' '+(r.point>0?'+':'')+r.point;if(mk==='totals')return r.name+' '+r.point;return r.name}
const MK={h2h:'Moneyline',spreads:'Spread',totals:'Total'};
function rowEv(r){let b=null;if(r.fair==null)return b;for(const v of V){const d=r.prices[v];if(d){const e=r.fair/cost(v,d)-1;if(b===null||e>b)b=e}}return b}
function pill(e){if(e===null)return '<span class="ev no">n/a</span>';const k=e>=D.bar?'':e>D.bar-0.015?'near':'no';
  return `<span class="ev ${k}">${e>0?'+':''}${(e*100).toFixed(1)}%</span>`}
const books=[...V,...D.books.filter(b=>!V.includes(b))];
function render(){
  let evs=[];for(const sk in D.sports){if(S.sport&&S.sport!=='all'&&S.sport!==sk)continue;
    for(const e of D.sports[sk].events)evs.push({...e,_at:D.sports[sk].at})}
  evs=evs.filter(e=>new Date(e.start)>now);
  if(S.venueOnly!==false)evs=evs.filter(e=>Object.values(e.markets).some(m=>m.rows.some(r=>V.some(v=>r.prices[v]))));
  evs.forEach(e=>{e._best=-9;for(const mk in e.markets)for(const r of e.markets[mk].rows){const x=rowEv(r);if(x!==null)e._best=Math.max(e._best,x)}});
  evs.sort(S.sort==='ev'?(a,b)=>b._best-a._best:(a,b)=>new Date(a.start)-new Date(b.start));
  $('count').textContent=evs.length+(evs.length===1?' game':' games');
  const ncol=4+books.length;
  let h='<thead><tr><th>EV</th><th>Pick</th><th>Market</th><th class="c">Fair</th>'+
    books.map(b=>`<th class="c${V.includes(b)?' venue':''}">${L[b]||b}</th>`).join('')+'</tr></thead><tbody>';
  for(const e of evs.slice(0,150)){
    h+=`<tr class="game"><td colspan="${ncol}"><b>${e.away} @ ${e.home}</b>${e.sport}, ${when(e.start)}. Odds from ${ago(e._at)}.</td></tr>`;
    for(const mk of ['h2h','spreads','totals']){const m=e.markets[mk];if(!m)continue;if(S.market&&S.market!=='all'&&S.market!==mk)continue;
      for(const r of m.rows){const ds=books.map(b=>r.prices[b]||0),mx=Math.max(...ds),many=ds.filter(y=>y).length>1;
        h+=`<tr><td>${pill(rowEv(r))}</td><td class="pick nw">${pick(mk,r)}</td><td class="sub nw">${MK[mk]}</td>`+
          `<td class="fair" title="Blended from ${r.n||0} sharp books">${r.fair?am(1/r.fair):'n/a'}</td>`+
          books.map(b=>{const d=r.prices[b],ven=V.includes(b);if(!d)return `<td class="px muted${ven?' venue':''}">-</td>`;
            let x=`<td class="px${d===mx&&many?' best':''}${ven?' venue':''}"${ven?` title="${Math.round(100/d)}¢"`:''}>${am(d)}`;
            if(ven&&r.fair){const ev=r.fair/cost(b,d)-1;x+=`<span class="e ${ev>0?'pos':'muted'}">${(ev*100).toFixed(1)}%</span>`}
            return x+'</td>'}).join('')+'</tr>'}}
  }
  h+='</tbody>';
  $('out').innerHTML=evs.length?`<div class="scroll"><table>${h}</table></div>`:
    '<div class="empty">No upcoming games match these filters. Odds fill in as the engine checks each sport.</div>';
}
function chips(id,key,def){const box=$(id);box.querySelectorAll('button').forEach(b=>{
  b.setAttribute('aria-pressed',(S[key]||def)===b.dataset.v);
  b.onclick=()=>{S[key]=b.dataset.v;save();chips(id,key,def);render()}})}
const sel=$('sport');for(const sk in D.sports){const o=document.createElement('option');o.value=sk;
  o.textContent=(D.sports[sk].events[0]||{}).sport||sk;sel.appendChild(o)}
sel.value=S.sport||'all';sel.onchange=e=>{S.sport=e.target.value;save();render()};
chips('market','market','all');chips('sort','sort','time');
const vo=$('venueOnly');vo.setAttribute('aria-pressed',S.venueOnly!==false);
vo.onclick=()=>{S.venueOnly=!(S.venueOnly!==false);vo.setAttribute('aria-pressed',S.venueOnly);save();render()};
render();
"""


def render_odds(state, cfg):
    books = book_list(cfg)
    data = {"now": datetime.now(timezone.utc).isoformat(), "fees": cfg["fees"], "venues": cfg["venues"],
            "books": books, "labels": BOOK_LABEL, "sports": state.get("board", {})}
    data["bar"] = min(cfg["min_ev"].values())
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    body = (f'<style>{ODDS_CSS}</style><h1>Odds screen</h1><p class="lede">Fair is the no-vig price blended from '
            'Pinnacle, Betfair, BetOnline, Novig and ProphetX. The EV badge is the best Kalshi or Poly US edge '
            'after fees. Green is the best price in the row.</p>'
            '<div class="oddsbar"><div class="chips"><select id="sport" aria-label="Sport">'
            '<option value="all">All sports</option></select>'
            '<span id="market" class="chips"><button data-v="all">All</button><button data-v="h2h">Moneyline</button>'
            '<button data-v="spreads">Spread</button><button data-v="totals">Total</button></span></div>'
            '<div class="chips"><span id="sort" class="chips"><button data-v="time">Soonest</button>'
            '<button data-v="ev">Best EV</button></span><button id="venueOnly">Only on Kalshi or Poly</button>'
            '<span class="sub" id="count"></span></div></div>'
            '<div class="panel" id="out"></div>'
            f'<script type="application/json" id="data">{blob}</script>')
    doc = ui.shell("Edge Finder odds", "odds", body, state, script=ODDS_JS)
    ODDS_OUT.write_text(doc)
