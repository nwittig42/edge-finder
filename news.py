"""News watcher: polls ESPN's free injury feeds and reports status changes.

Why it matters: when a key player is ruled out, Pinnacle reprices within seconds while
Kalshi / Polymarket US often lag. A change triggers an immediate odds check for that sport.
It also protects us: if a team has news newer than Pinnacle's last line update, the fair
price may be stale, so we don't bet that game until Pinnacle catches up.
"""

import csv
import json
import unicodedata
import urllib.request
from datetime import datetime, timedelta

from edge_finder import HERE, parse_time

ESPN = "https://site.api.espn.com/apis/site/v2/sports/{}/injuries"
LEAGUES = {"basketball_nba": "basketball/nba", "americanfootball_nfl": "football/nfl",
           "icehockey_nhl": "hockey/nhl"}
POLL_MINUTES = 3
NEWS_CSV = HERE / "logs" / "news.csv"
FIELDS = ["seen_at", "sport_key", "team", "player", "old", "new", "espn_time", "comment"]


def norm(name):
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()


def team_key(sport_key, team):
    return f"{sport_key}|{norm(team)}"


def fetch(path):
    req = urllib.request.Request(ESPN.format(path), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def poll(state, now, log):
    """Returns the set of sport keys with fresh, relevant news."""
    snap = state.setdefault("news_snapshot", {})
    last = state.setdefault("news_polled", {})
    team_news = state.setdefault("news_team", {})
    feed = state.setdefault("news", [])
    triggered = set()
    for sport_key, path in LEAGUES.items():
        if last.get(sport_key) and now - parse_time(last[sport_key]) < timedelta(minutes=POLL_MINUTES):
            continue
        try:
            data = fetch(path)
        except Exception as e:
            log(f"news {sport_key} failed: {e}")
            continue
        last[sport_key] = now.isoformat()
        cur, info = {}, {}
        for t in data.get("injuries", []):
            team = t.get("displayName", "")
            for i in t.get("injuries", []):
                k = f"{team}|{i['athlete']['displayName']}"
                cur[k] = i.get("status", "")
                info[k] = (i.get("date", ""), (i.get("shortComment") or "")[:200])
        prev = snap.get(sport_key)
        snap[sport_key] = cur
        if prev is None:
            continue  # first poll is the baseline, not news
        changes = []
        for k in set(prev) | set(cur):
            old, new = prev.get(k), cur.get(k)
            if old == new:
                continue
            team, player = k.split("|", 1)
            changes.append({"seen_at": now.isoformat(), "sport_key": sport_key, "team": team,
                            "player": player, "old": old or "(healthy)", "new": new or "(cleared)",
                            "espn_time": info.get(k, ("", ""))[0], "comment": info.get(k, ("", ""))[1]})
        if not changes:
            continue
        triggered.add(sport_key)
        for c in changes:
            team_news[team_key(sport_key, c["team"])] = now.isoformat()
        feed.extend(changes)
        del feed[:-150]
        new_file = not NEWS_CSV.exists()
        with NEWS_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(changes)
        log(f"news {sport_key}: {len(changes)} change(s), e.g. {changes[0]['player']} "
            f"{changes[0]['old']} -> {changes[0]['new']}")
    return triggered


def latest_team_news(state, sport_key, *teams):
    ts = [state.get("news_team", {}).get(team_key(sport_key, t)) for t in teams]
    ts = [parse_time(t) for t in ts if t]
    return max(ts) if ts else None
