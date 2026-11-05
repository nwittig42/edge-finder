"""Shared look for every Edge Finder page: one sidebar, one type scale, one table style.
Modeled on odds-screen tools: the +EV table is the product, everything else is secondary."""

import html
from datetime import datetime, timezone

CSS = """
:root{--bg:#f4f6f9;--panel:#fff;--ink:#0f1726;--muted:#667085;--line:#e4e7ec;--side:#0c1424;--side-ink:#c7cfdb;
--side-act:#1b2740;--brand:#12b76a;--pos:#039855;--pos-bg:#e6f6ee;--neg:#d92d20;--neg-bg:#fdecea;--warn:#b54708;
--warn-bg:#fef4e6;--accent:#2e6be6;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#0b111c;--panel:#111a29;--ink:#e6eaf0;--muted:#8a94a6;--line:#1f2a3c;
--side:#070c15;--side-act:#16213a;--pos:#32d583;--pos-bg:#0f2e22;--neg:#f97066;--neg-bg:#3a1714;--warn:#fdb022;
--warn-bg:#36260b;--accent:#6c9cff;--chip:#1a2436}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",
"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.app{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.side{background:var(--side);color:var(--side-ink);padding:20px 12px;position:sticky;top:0;height:100vh;
display:flex;flex-direction:column;gap:2px}
.logo{color:#fff;font-weight:700;font-size:17px;padding:0 10px 18px;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.logo b{color:var(--brand);font-weight:700}
.side a{color:var(--side-ink);padding:9px 10px;border-radius:8px;font-size:14px;font-weight:500}
.side a:hover{background:var(--side-act);text-decoration:none;color:#fff}
.side a.on{background:var(--side-act);color:#fff;box-shadow:inset 3px 0 0 var(--brand)}
.side .foot{margin-top:auto;font-size:12px;line-height:1.6;padding:0 10px;color:#8592a6}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
main{padding:28px 32px 60px;max-width:1240px;width:100%;min-width:0}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.015em}
.lede{color:var(--muted);margin:0 0 20px;max-width:70ch}
h2{font-size:15px;margin:0}
.stats{display:flex;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:12px;
margin-bottom:20px}
.stat{flex:1 1 150px;padding:14px 18px;border-right:1px solid var(--line)}.stat:last-child{border-right:0}
.stat .l{color:var(--muted);font-size:12.5px}.stat .v{font-size:22px;font-weight:650;margin-top:2px}
.stat .s{color:var(--muted);font-size:12px;margin-top:1px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:20px;overflow:hidden}
.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;
border-bottom:1px solid var(--line);flex-wrap:wrap}
.ph .hint{color:var(--muted);font-size:12.5px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;padding:9px 14px;background:var(--bg);
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}tbody tr:hover td{background:color-mix(in srgb,var(--bg) 60%,transparent)}
.r{text-align:right}.c{text-align:center}.nw{white-space:nowrap}
.sub{color:var(--muted);font-size:12.5px}.muted{color:var(--muted)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}
.ev{display:inline-block;min-width:56px;text-align:center;font-weight:650;font-size:13px;padding:4px 8px;
border-radius:6px;background:var(--pos-bg);color:var(--pos)}
.ev.near{background:var(--warn-bg);color:var(--warn)}.ev.no{background:var(--chip);color:var(--muted)}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--chip);
color:var(--muted)}.tag.pos{background:var(--pos-bg);color:var(--pos)}.tag.neg{background:var(--neg-bg);color:var(--neg)}
.tag.warn{background:var(--warn-bg);color:var(--warn)}
.pick{font-weight:600}
.empty{padding:28px 18px;color:var(--muted)}
.tabs{display:flex;gap:4px;padding:6px 8px 0;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs button{font:inherit;font-weight:600;color:var(--muted);background:none;border:0;padding:10px 12px;cursor:pointer;
border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}
.tabs button[aria-selected=true]{color:var(--ink);border-color:var(--brand)}
.tabs button .n{font-weight:500;color:var(--muted);margin-left:4px}
[role=tabpanel][hidden]{display:none}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips button,.chips select{font:inherit;font-size:13px;font-weight:500;color:var(--ink);background:var(--panel);
border:1px solid var(--line);border-radius:99px;padding:6px 12px;cursor:pointer}
.chips button[aria-pressed=true]{background:var(--ink);color:var(--panel);border-color:var(--ink)}
button:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
details>summary{cursor:pointer;padding:14px 18px;font-weight:600;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"+";display:inline-block;width:18px;color:var(--muted)}
details[open]>summary::before{content:"\\2212"}
pre{white-space:pre-wrap;font:12.5px/1.55 ui-monospace,Menlo,monospace;margin:0;padding:16px 18px}
.prose{padding:6px 24px 20px;max-width:860px}.prose p,.prose li{line-height:1.6}
.prose h1,.prose h2,.prose h3{margin:24px 0 8px}.prose table{margin:8px 0}
.prose code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px}
svg text{fill:var(--muted);font-size:11px}
@media(max-width:860px){.app{grid-template-columns:minmax(0,1fr)}
.side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;padding:10px 12px;gap:2px}
.side a{white-space:nowrap}
.logo{padding:0 10px 0 4px;width:100%;margin-bottom:6px}.side .foot{display:none}
.side a{padding:7px 10px;font-size:13px}main{padding:20px 16px 48px}
.stat{flex-basis:50%;border-right:0;border-bottom:1px solid var(--line)}
.ph .hint{width:100%}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

TABS_JS = """
document.querySelectorAll('[data-tabs]').forEach(box=>{
  const key='ef_tab_'+box.dataset.tabs, btns=[...box.querySelectorAll('[role=tab]')];
  function show(id){btns.forEach(b=>{const on=b.dataset.t===id;b.setAttribute('aria-selected',on);
    document.getElementById(b.dataset.t).hidden=!on});try{localStorage.setItem(key,id)}catch(e){}}
  btns.forEach(b=>b.onclick=()=>show(b.dataset.t));
  let saved=null;try{saved=localStorage.getItem(key)}catch(e){}
  show(btns.some(b=>b.dataset.t===saved)?saved:btns[0].dataset.t);
});
"""

NAV = [("bets", "/dashboard.html", "+EV bets"), ("odds", "/odds.html", "Odds screen"), ("track", "/tracking", "Paper tracking"),
       ("golive", "/monitor", "Go-live check"), ("log", "/log", "Engine log"), ("plan", "/plan", "Plan")]


def esc(x):
    return html.escape(str(x))


def status_html(state):
    from edge_finder import parse_time
    last = (state or {}).get("last_cycle")
    fresh = bool(last) and (datetime.now(timezone.utc) - parse_time(last)).total_seconds() < 600
    cr = (state or {}).get("credits_remaining")
    return (f'<span class="dot" style="background:{"var(--brand)" if fresh else "var(--neg)"}"></span>'
            f'{"Engine running" if fresh else "Engine not responding"}<br>Paper mode<br>'
            f'{f"{cr:,}" if isinstance(cr, int) else "?"} API credits left')


def shell(title, active, body, state=None, refresh=60, script=""):
    links = "".join(f'<a href="{href}" class="{"on" if key == active else ""}"'
                    f'{" aria-current=page" if key == active else ""}>{label}</a>' for key, href, label in NAV)
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{esc(title)}</title><link rel="icon" type="image/png" href="/static/favicon.png"><style>{CSS}</style></head><body><div class="app">'
            f'<nav class="side" aria-label="Pages"><div class="logo"><img src="/static/logo.png" alt="" width="30" height="30"><span>Edge<b>Finder</b></span></div>{links}'
            f'<div class="foot">{status_html(state)}</div></nav>'
            f'<main>{body}</main></div><script>{TABS_JS}{script}</script></body></html>')


def stats(items):
    return '<div class="stats">' + "".join(
        f'<div class="stat"><div class="l">{a}</div><div class="v">{b}</div><div class="s">{c}</div></div>'
        for a, b, c in items) + "</div>"


def tabs(group, panes):
    """panes: list of (id, label, count_or_None, html)."""
    btns = "".join(f'<button role="tab" data-t="{i}">{lab}'
                   f'{f"<span class=n>{n}</span>" if n is not None else ""}</button>' for i, lab, n, _ in panes)
    bodies = "".join(f'<div role="tabpanel" id="{i}">{h}</div>' for i, _, _, h in panes)
    return f'<div class="panel" data-tabs="{group}"><div class="tabs" role="tablist">{btns}</div>{bodies}</div>'


def ev_pill(ev, bar=None):
    k = "" if (bar is None and ev > 0) or (bar is not None and ev >= bar) else \
        "near" if bar is not None and ev > bar - 0.015 else "no"
    return f'<span class="ev {k}">{ev:+.1%}</span>'
