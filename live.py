"""Phase 2: live Kalshi + Polymarket US prices for moneylines, spreads and totals (all free).

Streams (real time):
  Kalshi        wss ticker channel: best yes bid/ask + sizes for every matched market
  Polymarket US wss MARKET_DATA: full order book (best price + size) for every matched market
Polling (discovery + fallback if a stream drops):
  Kalshi REST market lists per series (game, spread, total), every 60 s (20 s if stream down)
  Polymarket US moneyline list (5 min) and each game's full market list (10 min), book fallback 30 s

Each venue market is matched to The Odds API game and line, then priced against the blended sharp
fair price from the latest odds check. If a live price clears the bar but the sharp prices are more
than a few minutes old, the engine spends 1 credit per market to re-check that one game first.
"""

import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from edge_finder import consensus_fair, effective_cost, parse_time
import soccer_model
from wsclient import WebSocket, kalshi_headers, pm_headers

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us"
ET = ZoneInfo("America/New_York")
K_SERIES = {  # sport -> (moneyline, spread, total)
    "americanfootball_nfl": ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"),
    "americanfootball_ncaaf": ("KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL"),
    "basketball_nba": ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"),
    "basketball_ncaab": ("KXNCAAMBGAME", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL"),
    "icehockey_nhl": ("KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL"),
    "soccer_epl": ("KXEPLGAME", "KXEPLSPREAD", "KXEPLTOTAL"),
    "soccer_usa_mls": ("KXMLSGAME", "KXMLSSPREAD", "KXMLSTOTAL"),
    "mma_mixed_martial_arts": ("KXUFCFIGHT", None, None),
    "soccer_spain_la_liga": ('KXLALIGAGAME', 'KXLALIGASPREAD', 'KXLALIGATOTAL'),
    "soccer_germany_bundesliga": ('KXBUNDESLIGAGAME', 'KXBUNDESLIGASPREAD', 'KXBUNDESLIGATOTAL'),
    "soccer_italy_serie_a": ('KXSERIEAGAME', 'KXSERIEASPREAD', 'KXSERIEATOTAL'),
    "soccer_france_ligue_one": ('KXLIGUE1GAME', 'KXLIGUE1SPREAD', 'KXLIGUE1TOTAL'),
    "soccer_uefa_champs_league": ('KXUCLGAME', 'KXUCLSPREAD', 'KXUCLTOTAL'),
    "soccer_uefa_europa_league": ('KXUELGAME', 'KXUELSPREAD', 'KXUELTOTAL'),
    "soccer_uefa_europa_conference_league": ('KXUECLGAME', 'KXUECLSPREAD', 'KXUECLTOTAL'),
    "soccer_efl_champ": ('KXEFLCHAMPIONSHIPGAME', 'KXEFLCHAMPIONSHIPSPREAD', 'KXEFLCHAMPIONSHIPTOTAL'),
    "soccer_mexico_ligamx": ('KXLIGAMXGAME', 'KXLIGAMXSPREAD', 'KXLIGAMXTOTAL'),
    "soccer_netherlands_eredivisie": ('KXEREDIVISIEGAME', 'KXEREDIVISIESPREAD', 'KXEREDIVISIETOTAL'),
    "soccer_portugal_primeira_liga": ('KXLIGAPORTUGALGAME', 'KXLIGAPORTUGALSPREAD', 'KXLIGAPORTUGALTOTAL'),
    "soccer_germany_bundesliga2": ('KXBUNDESLIGA2GAME', 'KXBUNDESLIGA2SPREAD', 'KXBUNDESLIGA2TOTAL'),
}
FRESH_FAIR_MIN = 3        # sharp prices younger than this are trusted without a re-check
CONFIRM_GAP_MIN = 5       # don't re-check the same game more often than this
QUOTE_MAX_AGE = 120       # seconds; older live quotes are ignored
STREAM_STALE = 180        # seconds without a stream message = treat stream as down
MARKET_LABEL = {"h2h": "", "spreads": "spread", "totals": "total"}

_t = {"kalshi": 0, "pm_list": 0, "map": 0, "pm_book": 0}
_kidx = {}      # game code -> {"sport", "date", "game": {sub: ticker}, "spread": {(sub, strike): ticker}, "total": {strike: ticker}}
_kq = {}        # ticker -> {"yes": cost, "yes_size", "no": cost, "no_size", "at"}
_pm_ml = []     # Polymarket US moneyline markets
_pm_games = {}  # game key -> {"at", "spreads": {(team, line): slug}, "totals": {line: slug}}
_pmq = {}       # slug -> {"long": cost, "long_size", "short": cost, "short_size", "at"}
MAP = {}        # odds event id -> {"sport", "names", "kcode", "kassign", "pm_game", "pm_ml"}
STREAMS = {}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "edge-finder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


ALIASES = {"southern miss": "southern mississippi", "miami fl": "miami", "ul monroe": "ul monroe",
           "louisiana monroe": "ul monroe", "ole miss": "mississippi", "uconn": "connecticut",
           "pitt": "pittsburgh", "umass": "umass", "unlv": "unlv", "utsa": "utsa", "uab": "uab",
           "fiu": "florida international", "fau": "florida atlantic", "app st": "appalachian st",
           "southern cal": "usc", "lsu": "lsu", "byu": "byu", "tcu": "tcu", "smu": "smu"}


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\bstate\b", "st", s)
    return re.sub(r"\s+", " ", s).strip()


def name_score(sub, team):
    """Kalshi short names: 'Los Angeles C', 'North Dakota St.', 'Utah'. Lower score = better; None = no match."""
    sub, team = norm(sub), norm(team)
    sub = ALIASES.get(sub, sub)
    if sub in ("tie", "draw"):
        return 0 if team == "draw" else None
    if sub == team:
        return 0
    words = sub.split()
    if len(words) > 1 and len(words[-1]) == 1:   # trailing initial disambiguates the nickname
        base, initial = " ".join(words[:-1]), words[-1]
        if not team.startswith(base + " "):
            return None
        rest = team[len(base):].split()
        return len(rest) if rest and rest[0][0] == initial else None
    if team.startswith(sub + " "):
        return len(team[len(sub):].split())
    if sub.split()[-1:] == team.split()[-1:] and len(sub.split()) >= 2:   # fighters: same surname
        return 1
    return _fuzzy(sub, team)


CLUB_WORDS = {"fc", "cf", "ac", "as", "rc", "sc", "afc", "cd", "ud", "sd", "vfb", "vfl", "fsv", "tsg", "sv", "ogc",
              "losc", "stade", "1", "29", "de", "la", "club", "calcio", "sk", "fk", "bk", "cp", "sporting", "real"}
TOKEN_ALIASES = {"rennais": "rennes", "mgladbach": "monchengladbach", "gladbach": "monchengladbach",
                 "inter": "internazionale", "spurs": "tottenham", "wolves": "wolverhampton", "atleti": "atletico"}


def _fuzzy(sub, team):
    """Soccer-style short names ('PSG', 'Atletico', 'Bilbao', 'Stade Rennais'). Only used as a fallback; a game
    still has to match every outcome, on the same date and league, so a loose name match can't cross games."""
    full = team.split()
    if len(sub) in (2, 3, 4) and " " not in sub and sub == "".join(w[0] for w in full if w):
        return 2   # acronym, e.g. PSG = Paris Saint Germain
    st = [TOKEN_ALIASES.get(w, w) for w in sub.split() if w not in CLUB_WORDS] or sub.split()
    tt = [TOKEN_ALIASES.get(w, w) for w in full if w not in CLUB_WORDS] or full
    hit = sum(any(a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a)) for b in tt) for a in st)
    if hit == len(st):
        return 3
    if hit and hit * 2 >= len(st):
        return 4
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------- Kalshi ----------

def _kalshi_quote(ticker, yes_ask, yes_ask_size, yes_bid, yes_bid_size):
    """Buying YES costs the ask; buying NO costs 1 - yes bid, with the yes-bid size available."""
    _kq[ticker] = {"yes": yes_ask if 0 < yes_ask < 1 else None, "yes_size": yes_ask_size,
                   "no": 1 - yes_bid if 0 < yes_bid < 1 else None, "no_size": yes_bid_size, "at": time.time()}


def poll_kalshi(sports, log):
    for sport in sports:
        for kind, series in zip(("game", "spread", "total"), K_SERIES.get(sport, ())):
            if not series:
                continue
            cursor = ""
            for _ in range(8):
                q = {"series_ticker": series, "status": "open", "limit": 1000}
                if cursor:
                    q["cursor"] = cursor
                try:
                    d = _get(f"{KALSHI}/markets?{urllib.parse.urlencode(q)}")
                except Exception as e:
                    log(f"live kalshi {series} failed: {e}")
                    break
                for m in d.get("markets", []):
                    code = m["event_ticker"].split("-", 1)[1] if "-" in m["event_ticker"] else ""
                    e = _kidx.setdefault(code, {"sport": sport, "date": code[:7], "game": {}, "spread": {}, "total": {}})
                    sub = m.get("yes_sub_title", "")
                    if kind == "game":
                        e["game"][sub] = m["ticker"]
                    elif kind == "spread":
                        team = sub.split(" wins by")[0].strip()
                        e["spread"][(team, _f(m.get("floor_strike")))] = m["ticker"]
                    else:
                        e["total"][_f(m.get("floor_strike"))] = m["ticker"]
                    _kalshi_quote(m["ticker"], _f(m.get("yes_ask_dollars")), _f(m.get("yes_ask_size_fp")),
                                  _f(m.get("yes_bid_dollars")), _f(m.get("yes_bid_size_fp")))
                cursor = d.get("cursor") or ""
                if not cursor:
                    break


def _kalshi_msg(m):
    if m.get("type") == "ticker":
        x = m.get("msg", {})
        _kalshi_quote(x.get("market_ticker"), _f(x.get("yes_ask_dollars")), _f(x.get("yes_ask_size_fp")),
                      _f(x.get("yes_bid_dollars")), _f(x.get("yes_bid_size_fp")))


# ---------- Polymarket US ----------

def poll_pm_list(log):
    global _pm_ml
    out = []
    for off in range(0, 2000, 100):
        try:
            ms = _get(f"{PMUS}/v1/markets?limit=100&offset={off}&active=true&closed=false&marketTypes=moneyline").get("markets", [])
        except Exception as e:
            log(f"live polymarket list failed: {e}")
            return
        out += ms
        if len(ms) < 100:
            break
    _pm_ml = out


def game_key(slug):
    """'aec-nba-phi-det-2026-11-04' / 'asc-nfl-jax-bal-2026-11-05-neg-27pt5' -> 'nba-phi-det-2026-11-04'."""
    p = slug.split("-")
    return "-".join(p[1:7]) if len(p) >= 7 else None


def poll_pm_game(gk, log):
    """All full-game spreads and totals for one Polymarket US game."""
    try:
        e = _get(f"{PMUS}/v1/events/slug/{gk}", timeout=20)
    except Exception as ex:
        log(f"live polymarket game {gk} failed: {ex}")
        return
    e = e.get("event", e)
    g = {"at": time.time(), "spreads": {}, "totals": {}}
    for m in e.get("markets") or []:
        smt = m.get("sportsMarketType") or ""
        if not m.get("active", True) or m.get("closed"):
            continue
        if smt.endswith("_full_game_spread"):
            long_side = next((s for s in m.get("marketSides") or [] if s.get("long")), None)
            team = ((long_side or {}).get("team") or {}).get("name")
            if team and m.get("line") is not None:
                g["spreads"][(norm(team), float(m["line"]))] = m["slug"]
        elif smt.endswith("_full_game_total") and "points" not in smt and "team_total" not in smt:
            if m.get("line") is not None:
                g["totals"][float(m["line"])] = m["slug"]
    _pm_games[gk] = g


def _pm_book(md):
    slug = md.get("marketSlug")
    bids, offers = md.get("bids") or [], md.get("offers") or []
    best_offer = min(offers, key=lambda o: _f(o["px"]["value"])) if offers else None
    best_bid = max(bids, key=lambda o: _f(o["px"]["value"])) if bids else None
    _pmq[slug] = {"long": _f(best_offer["px"]["value"]) if best_offer else None,
                  "long_size": _f(best_offer["qty"]) if best_offer else 0,
                  "short": 1 - _f(best_bid["px"]["value"]) if best_bid else None,
                  "short_size": _f(best_bid["qty"]) if best_bid else 0, "at": time.time()}


def _pm_msg(m):
    if m.get("marketData"):
        _pm_book(m["marketData"])


def poll_pm_books(slugs, log):
    for slug in slugs:
        try:
            _pm_book(_get(f"{PMUS}/v1/markets/{slug}/book").get("marketData", {}))
        except Exception as e:
            log(f"live polymarket book {slug} failed: {e}")
        time.sleep(0.05)   # far under the 25 req/s limit


# ---------- streams ----------

class Stream(threading.Thread):
    """Keeps one WebSocket open, subscribed to the current target set; reconnects on change or error."""

    def __init__(self, name, url, path, headers_fn, sub_fn, on_msg, log, chunk):
        super().__init__(daemon=True, name=name)
        self.url, self.path, self.headers_fn, self.sub_fn = url, path, headers_fn, sub_fn
        self.on_msg, self.log, self.chunk = on_msg, log, chunk
        self.targets, self.wanted = frozenset(), frozenset()
        self.last_msg, self.connected, self.errors = 0.0, False, 0

    def set_targets(self, targets):
        self.wanted = frozenset(t for t in targets if t)

    def healthy(self):
        return self.connected and time.time() - self.last_msg < STREAM_STALE

    def run(self):
        backoff = 2
        while True:
            if not self.wanted:
                time.sleep(2)
                continue
            ws = None
            try:
                self.targets = self.wanted
                ws = WebSocket(self.url, self.headers_fn("GET", self.path), timeout=30)
                items = sorted(self.targets)
                for i in range(0, len(items), self.chunk):
                    ws.send_json(self.sub_fn(i // self.chunk + 1, items[i:i + self.chunk]))
                self.connected, self.last_msg, backoff = True, time.time(), 2
                self.log(f"stream {self.name}: connected, {len(items)} markets")
                while self.targets == self.wanted:
                    self.on_msg(ws.recv_json())
                    self.last_msg = time.time()
            except Exception as e:
                self.errors += 1
                if self.targets == self.wanted:   # a real failure, not a planned resubscribe
                    self.log(f"stream {self.name}: {type(e).__name__} {str(e)[:150]}; reconnecting in {backoff}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 120)
            finally:
                self.connected = False
                if ws:
                    ws.close()


def start_streams(log):
    if STREAMS:
        return
    STREAMS["kalshi"] = Stream(
        "kalshi", "wss://api.elections.kalshi.com/trade-api/ws/v2", "/trade-api/ws/v2", kalshi_headers,
        lambda i, ts: {"id": i, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_tickers": ts}},
        _kalshi_msg, log, 200)
    STREAMS["polymarket_us"] = Stream(
        "polymarket_us", "wss://api.polymarket.us/v1/ws/markets", "/v1/ws/markets", pm_headers,
        lambda i, ss: {"subscribe": {"requestId": f"md-{i}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                     "marketSlugs": ss}},
        _pm_msg, log, 50)
    for s in STREAMS.values():
        s.start()


def _healthy(venue):
    s = STREAMS.get(venue)
    return bool(s and s.healthy())


# ---------- matching ----------

def build_map(board):
    """Match venue games to Odds API games. board = STATE['board'] (per sport, latest odds check)."""
    MAP.clear()
    for sport, b in board.items():
        for ev in b["events"]:
            names = [r["name"] for r in ev["markets"].get("h2h", {}).get("rows", [])]
            if not names:
                continue
            st = parse_time(ev["start"])
            m = MAP[ev["id"]] = {"sport": sport, "names": names, "home": ev["home"], "away": ev["away"]}
            # Kalshi: same ET date code; every outcome matched to a distinct moneyline sub-title
            code = st.astimezone(ET).strftime("%y%b%d").upper()
            best = None
            for kc, ke in _kidx.items():
                if ke["sport"] != sport or ke["date"] != code or not ke["game"]:
                    continue
                assign, total = {}, 0
                for n in names:
                    scored = [(s, sub) for sub in ke["game"] if (s := name_score(sub, n)) is not None
                              and sub not in assign.values()]
                    if not scored:
                        break
                    s, sub = min(scored)
                    assign[n], total = sub, total + s
                if len(assign) == len(names) and (best is None or total < best[0]):
                    best = (total, kc, assign)
            if best:
                m["kcode"], m["kassign"] = best[1], best[2]
            # Polymarket US: full team names + start time within 6 h
            for pm in _pm_ml:
                gs = pm.get("gameStartTime")
                if not gs or abs((parse_time(gs) - st).total_seconds()) > 6 * 3600:
                    continue
                sides = {}
                for sd in pm.get("marketSides") or []:
                    t = sd.get("team") or {}
                    cands = {norm(t.get("name", "")), norm(t.get("safeName", "")), norm(sd.get("description", ""))}
                    for n in names:
                        if norm(n) in cands:
                            sides[n] = "long" if sd.get("long") else "short"
                if len(sides) == len(names) == 2:
                    m["pm_ml"] = {n: (pm["slug"], side) for n, side in sides.items()}
                    m["pm_game"] = game_key(pm["slug"])
                    break


def _ref(event_id, market, name, point, venue):
    """Which venue contract (and side) is this exact Odds API outcome? None if not offered."""
    m = MAP.get(event_id)
    if not m:
        return None
    names = m["names"]
    opp = next((n for n in names if n != name), None) if len(names) == 2 else None
    if venue == "kalshi" and m.get("kcode"):
        k = _kidx.get(m["kcode"], {})
        a = m["kassign"]
        if market == "h2h":
            t = k.get("game", {}).get(a.get(name))
            return (t, "yes") if t else None
        if market == "spreads" and point is not None and opp:
            if point < 0:
                t = k.get("spread", {}).get((a.get(name), -point))
                return (t, "yes") if t else None
            t = k.get("spread", {}).get((a.get(opp), point))
            return (t, "no") if t else None
        if market == "totals" and point is not None:
            t = k.get("total", {}).get(point)
            return (t, "yes" if name == "Over" else "no") if t else None
    if venue == "polymarket_us":
        if market == "h2h":
            return m.get("pm_ml", {}).get(name)
        g = _pm_games.get(m.get("pm_game"))
        if not g or point is None:
            return None
        if market == "spreads" and opp:
            s = g["spreads"].get((norm(name), point))
            if s:
                return (s, "long")
            s = g["spreads"].get((norm(opp), -point))
            return (s, "short") if s else None
        if market == "totals":
            s = g["totals"].get(point)
            return (s, "long" if name == "Over" else "short") if s else None
    return None


def live_quote(event_id, market, name, point, venue):
    """Current live price to BUY this outcome: {"cost_raw", "size", "at", "ref"} or None."""
    r = _ref(event_id, market, name, point, venue)
    if not r:
        return None
    key, side = r
    q = (_kq if venue == "kalshi" else _pmq).get(key)
    if not q or not q.get(side) or time.time() - q["at"] > QUOTE_MAX_AGE:
        return None
    return {"cost_raw": q[side], "size": q.get(f"{side}_size"), "at": q["at"], "ref": f"{key}:{side}"}


def _targets(board):
    k, p = set(), set()
    for b in board.values():
        for ev in b["events"]:
            if ev["id"] not in MAP:
                continue
            for mk, mkt in ev["markets"].items():
                for r in mkt["rows"]:
                    if r.get("fair") is None:
                        continue
                    rk = _ref(ev["id"], mk, r["name"], r.get("point"), "kalshi")
                    rp = _ref(ev["id"], mk, r["name"], r.get("point"), "polymarket_us")
                    if rk:
                        k.add(rk[0])
                    if rp:
                        p.add(rp[0])
    return k, p


# ---------- edge check ----------

def tick(state, cfg, now, get_event_odds, log):
    """Called every ~10 s by the engine. Returns hits in the same format as scan_event."""
    start_streams(log)
    t = time.time()
    board = state.get("board", {})
    sports = [s for s in cfg["sports"] if s in board]
    k_every = 60 if _healthy("kalshi") else 20
    if t - _t["kalshi"] >= k_every:
        _t["kalshi"] = t
        poll_kalshi(sports, log)
    if t - _t["pm_list"] >= 300:
        _t["pm_list"] = t
        poll_pm_list(log)
    if t - _t["map"] >= 120 or not MAP:
        _t["map"] = t
        build_map(board)
        horizon = cfg["max_hours_to_start"] * 3600
        for eid, m in MAP.items():
            gk = m.get("pm_game")
            ev = next((e for b in board.values() for e in b["events"] if e["id"] == eid), None)
            if gk and ev and 0 < (parse_time(ev["start"]).timestamp() - t) < horizon and \
                    t - _pm_games.get(gk, {}).get("at", 0) > 600:
                poll_pm_game(gk, log)
        kt, pt = _targets(board)
        STREAMS["kalshi"].set_targets(kt)
        STREAMS["polymarket_us"].set_targets(pt)
    if not _healthy("polymarket_us") and t - _t["pm_book"] >= 30:
        _t["pm_book"] = t
        poll_pm_books(sorted(STREAMS["polymarket_us"].wanted), log)

    state["live_status"] = {
        "at": now.isoformat(), "events": len(MAP),
        "kalshi_games": sum("kcode" in m for m in MAP.values()),
        "pm_games": sum("pm_ml" in m for m in MAP.values()),
        "kalshi_markets": len(STREAMS["kalshi"].wanted), "pm_markets": len(STREAMS["polymarket_us"].wanted),
        "kalshi_stream": "live" if _healthy("kalshi") else "polling",
        "pm_stream": "live" if _healthy("polymarket_us") else "polling"}

    hits = []
    confirms = state.setdefault("live_confirms", {})
    day = datetime.now().date().isoformat()
    if state.get("live_confirm_day") != day:
        state["live_confirm_day"], state["live_confirm_used"] = day, 0
    for sport, b in board.items():
        for ev in b["events"]:
            if ev["id"] not in MAP:
                continue
            st = parse_time(ev["start"])
            mins = (st - now).total_seconds() / 60
            if mins < cfg["min_minutes_to_start"] or mins > cfg["max_hours_to_start"] * 60:
                continue
            cands = _candidates(ev, cfg)
            if not cands:
                continue
            age_min = (now - parse_time(b["at"])).total_seconds() / 60
            sharp_updated = {mk: m["updated"].get(cfg["sharp_book"], b["at"]) for mk, m in ev["markets"].items()}
            if age_min > FRESH_FAIR_MIN:
                last = confirms.get(ev["id"])
                if last and (now - parse_time(last)).total_seconds() / 60 < CONFIRM_GAP_MIN:
                    continue
                mks = sorted({c[0] for c in cands})
                if state["live_confirm_used"] + len(mks) > cfg.get("max_live_confirms_per_day", 60):
                    continue
                confirms[ev["id"]] = now.isoformat()
                state["live_confirm_used"] += len(mks)
                fresh = get_event_odds(sport, ev["id"], mks)
                if not fresh:
                    continue
                for mk in mks:
                    rows, upd = _fresh_rows(fresh, mk, cfg, now)
                    if upd:
                        sharp_updated[mk] = upd
                    for r in ev["markets"][mk]["rows"]:   # keep the board current too
                        if r.get("src") != "model":
                            r.update(rows.get((r["name"], r.get("point")), {"fair": None}))
                if sport.startswith("soccer"):
                    soccer_model.fill_board_event(ev)   # re-price model rows off the fresh sharp lines
                cands = _candidates(ev, cfg)
                log(f"live re-check {ev['away']} @ {ev['home']} ({','.join(mks)}): {len(cands)} edge(s) remain")
            for mk, r, venue, q, evp, bar in cands:
                pt = r.get("point")
                pick = r["name"] + (f" {pt:+g}" if mk == "spreads" else f" {pt:g}" if mk == "totals" else "")
                hits.append({
                    "event_id": ev["id"], "sport_key": sport, "name": r["name"], "point": pt,
                    "sport": ev["sport"], "event": f"{ev['away']} @ {ev['home']}",
                    "home": ev["home"], "away": ev["away"], "start": st, "market": mk, "pick": pick,
                    "venue": venue, "price": 1 / q["cost_raw"], "price_cents": round(100 * q["cost_raw"]),
                    "fair_p": r["fair"], "fair_odds": 1 / r["fair"], "ev": evp, "bar": bar,
                    "n_books": r.get("n", 0), "spread": r.get("spread") or 0,
                    "sharp_updated": sharp_updated.get(mk, b["at"]), "live": True,
                    "size": q["size"], "ref": q["ref"], "src": r.get("src")})
    return hits


def _candidates(ev, cfg):
    out = []
    for mk, mkt in ev["markets"].items():
        for r in mkt["rows"]:
            if not r.get("fair"):
                continue
            agree = r.get("n", 0) >= cfg.get("consensus_min_books", 99) and \
                (r.get("spread") if r.get("spread") is not None else 1) <= cfg.get("consensus_max_spread", 0)
            bar = (cfg.get("min_ev_consensus", {}) if agree else cfg["min_ev"]).get(mk, 0.03)
            for venue in cfg["venues"]:
                q = live_quote(ev["id"], mk, r["name"], r.get("point"), venue)
                if not q:
                    continue
                evp = r["fair"] / effective_cost(venue, 1 / q["cost_raw"], cfg) - 1
                if bar <= evp <= cfg["max_believable_ev"]:
                    out.append((mk, r, venue, q, evp, bar))
    return out


def _fresh_rows(ev, mk, cfg, now):
    books = {b["key"]: b for b in ev.get("bookmakers", [])}
    sharp = books.get(cfg["sharp_book"])
    m = sharp and next((m for m in sharp["markets"] if m["key"] == mk), None)
    if not m:
        return {}, None
    cons = consensus_fair(books, mk, m["outcomes"], cfg, now)
    rows = {k: {"fair": round(v[0], 4), "n": v[1], "spread": round(v[2], 4)} for k, v in cons.items()}
    return rows, m.get("last_update") or sharp.get("last_update") or now.isoformat()


def track_candidates(state, cfg, now):
    """Every live price with EV >= track_min_ev after fees: the paper tracking list (no bar, no texts)."""
    out, floor = [], cfg.get("track_min_ev", 0.0)
    for sport, b in state.get("board", {}).items():
        age_min = (now - parse_time(b["at"])).total_seconds() / 60
        for ev in b["events"]:
            if ev["id"] not in MAP:
                continue
            st = parse_time(ev["start"])
            mins = (st - now).total_seconds() / 60
            if mins < cfg["min_minutes_to_start"] or mins > cfg["max_hours_to_start"] * 60:
                continue
            for mk, mkt in ev["markets"].items():
                for r in mkt["rows"]:
                    if not r.get("fair"):
                        continue
                    agree = r.get("n", 0) >= cfg.get("consensus_min_books", 99) and \
                        (r.get("spread") if r.get("spread") is not None else 1) <= cfg.get("consensus_max_spread", 0)
                    bar = (cfg.get("min_ev_consensus", {}) if agree else cfg["min_ev"]).get(mk, 0.03)
                    for venue in cfg["venues"]:
                        q = live_quote(ev["id"], mk, r["name"], r.get("point"), venue)
                        if not q:
                            continue
                        evp = r["fair"] / effective_cost(venue, 1 / q["cost_raw"], cfg) - 1
                        if not (floor <= evp <= cfg["max_believable_ev"]):
                            continue
                        pt = r.get("point")
                        out.append({
                            "event_id": ev["id"], "sport_key": sport, "sport": ev["sport"],
                            "event": f"{ev['away']} @ {ev['home']}", "home": ev["home"], "away": ev["away"],
                            "start": ev["start"], "market": mk, "name": r["name"], "point": pt,
                            "pick": r["name"] + (f" {pt:+g}" if mk == "spreads" else f" {pt:g}" if mk == "totals" else ""),
                            "venue": venue, "price": 1 / q["cost_raw"], "fair_p": r["fair"], "ev": evp, "bar": bar,
                            "n_books": r.get("n", 0), "spread": r.get("spread"), "fair_age_min": round(age_min, 1),
                            "mins_to_start": round(mins), "size": q["size"], "ref": q["ref"],
                            "src": r.get("src")})
    return out
