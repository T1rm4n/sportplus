#!/usr/bin/env python3
"""Update SPORTPULS public-score snapshots on GitHub Actions.

ESPN's scoreboard API is undocumented and may change; this script intentionally
keeps the last known good data when a request fails. No secrets or dependencies.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "live.json"
LEAGUES = {
    "pl": ("soccer", "eng.1"),
    "ch": ("soccer", "eng.2"),
    "ll": ("soccer", "esp.1"),
    "bl": ("soccer", "ger.1"),
    "sa": ("soccer", "ita.1"),
    "all": ("soccer", "swe.1"),
    "nba": ("basketball", "nba"),
}
F1_ROOT = "https://api.jolpi.ca/ergast/f1/current"
HEADERS = {"User-Agent": "SPORTPULS/1.0 (+https://t1rm4n.github.io/sportplus/)", "Accept": "application/json"}


def fetch_json(url: str) -> dict:
    last_error = None
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=16) as res:
                obj = json.load(res)
            if not isinstance(obj, dict):
                raise ValueError("JSON response is not an object")
            return obj
        except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError) as err:
            last_error = err
            if attempt == 0:
                time.sleep(0.6)
    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


def normalize_match(event: dict) -> dict | None:
    comp = (event.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((x for x in competitors if x.get("homeAway") == "home"), None)
    away = next((x for x in competitors if x.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None

    def name(x):
        team = x.get("team") or {}
        return team.get("displayName") or team.get("name") or "Okänt lag"

    def points(x):
        value = x.get("score")
        if isinstance(value, dict):
            value = value.get("displayValue") or value.get("value")
        return str(value) if value is not None and str(value).strip() else None

    status = ((event.get("status") or {}).get("type") or {})
    state = status.get("state", "pre")
    if state not in {"pre", "in", "post"}:
        state = "pre"
    match = {
        "id": str(event.get("id") or ""),
        "start": event.get("date") or comp.get("date") or "",
        "home": name(home),
        "away": name(away),
        "home_score": points(home),
        "away_score": points(away),
        "state": state,
        "status": status.get("shortDetail") or status.get("description") or "",
    }
    return match if match["id"] and match["start"] else None


def update_league(sport: str, slug: str, dates: list[str]) -> list[dict]:
    results = {}
    success = 0
    errors = []
    for day in dates:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{slug}/scoreboard?dates={day}&limit=100"
        try:
            resp = fetch_json(url)
            if not isinstance(resp.get("events"), list):
                raise ValueError("Missing events list")
            for event in resp["events"]:
                item = normalize_match(event)
                if item:
                    results[item["id"]] = item
            success += 1
        except Exception as exc:
            logging.warning("%s / %s: %s", slug, day, exc)
            errors.append(day)
        time.sleep(0.12)
    # Don't silently publish a misleading partial feed: the older complete
    # snapshot remains if any date failed.
    if success != len(dates):
        raise RuntimeError(f"{slug}: only {success}/{len(dates)} days loaded: {', '.join(errors)}")
    return sorted(results.values(), key=lambda x: (x["start"], x["id"]))


def parse_f1_standings(data: dict, category: str) -> dict:
    table = (data.get("MRData") or {}).get("StandingsTable") or {}
    lists = table.get("StandingsLists") or []
    if not lists:
        raise ValueError(f"Missing F1 {category} standings")
    current = lists[0]
    field = "DriverStandings" if category == "drivers" else "ConstructorStandings"
    entries = []
    for row in current.get(field) or []:
        if category == "drivers":
            driver = row.get("Driver") or {}
            constructors = row.get("Constructors") or [{}]
            item = {
                "name": (driver.get("givenName", "") + " " + driver.get("familyName", "")).strip(),
                "team": constructors[0].get("name", "") if constructors else "",
                "number": driver.get("permanentNumber", ""),
                "wins": int(row.get("wins") or 0),
            }
        else:
            constructor = row.get("Constructor") or {}
            item = {"name": constructor.get("name", ""), "wins": int(row.get("wins") or 0)}
        item["position"] = int(row.get("position") or 0)
        item["points"] = float(row.get("points") or 0)
        if item["name"]:
            entries.append(item)
    if not entries:
        raise ValueError(f"Empty F1 {category} standings")
    return {"season": current.get("season", ""), "round": current.get("round", ""), "items": entries}




def _stat_map(stats: object) -> dict:
    """ESPN encodes standings stats in a list, with some fields present only on certain leagues."""
    out = {}
    if isinstance(stats, list):
        for stat in stats:
            if not isinstance(stat, dict):
                continue
            for label in (stat.get("name"), stat.get("type")):
                if isinstance(label, str) and label.strip():
                    out[label.casefold()] = stat
    return out


def _value(stats: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        item = stats.get(key.casefold())
        if not isinstance(item, dict):
            continue
        val = item.get("value")
        if val is None:
            val = item.get("displayValue")
        if val is not None and str(val).strip() not in ('', '--', '-'):
            return val
    return None


def _number(value: object) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace(',', '.').replace('%','').strip())
        if not (-1e8 < num < 1e8):
            return None
        return int(num) if num.is_integer() else round(num, 4)
    except (ValueError, TypeError):
        return None


def normalize_standings(payload: dict, sport: str) -> dict:
    """Extract season and table groups from ESPN's /apis/v2/ standings response.

    Preserve distinct NBA conferences. Do not invent missing stats as zero.
    """
    groups = []
    def visit(node: dict, inherited: str = '') -> None:
        if not isinstance(node, dict):
            return
        name = node.get('name') or node.get('displayName') or inherited
        raw = node.get('standings') or {}
        if isinstance(raw, dict):
            raw_entries = raw.get('entries') or []
        else:
            raw_entries = []
        if isinstance(raw_entries, list) and raw_entries:
            entries = []
            for idx, row in enumerate(raw_entries):
                if not isinstance(row, dict):
                    continue
                team = row.get('team') or {}
                title = team.get('displayName') or team.get('name') or team.get('shortDisplayName')
                if not title:
                    continue
                stats = _stat_map(row.get('stats'))
                rank = _number(_value(stats, ('rank','playoffSeed','seed')))
                if rank is None:
                    rank = _number(row.get('rank') or row.get('position'))
                item = {'name': title, 'id': str(team.get('id') or ''), 'rank': int(rank) if rank and rank > 0 else idx+1}
                if sport == 'soccer':
                    mapping = {
                        'p': ('gamesPlayed','played','matchesPlayed'),
                        'w': ('wins',), 'd': ('ties','draws'), 'l': ('losses',),
                        'gf': ('pointsFor','goalsFor','goalsScored'),
                        'ga': ('pointsAgainst','goalsAgainst','goalsConceded'),
                        'gd': ('pointDifferential','goalDifference','goalDifferential','differential'),
                        'pts': ('points',)
                    }
                else:
                    mapping = {
                        'w': ('wins',), 'l': ('losses',),
                        'pct': ('winPercent','winPercentage','winPct'),
                        'gb': ('gamesBehind','gamesBack'),
                        'diff': ('pointDifferential','differential'),
                    }
                for key, possibilities in mapping.items():
                    item[key] = _number(_value(stats, possibilities))
                if sport == 'soccer' and item['gd'] is None and item['gf'] is not None and item['ga'] is not None:
                    item['gd'] = item['gf'] - item['ga']
                entries.append(item)
            if entries:
                # Group order is meaningful, but explicit rank takes precedence.
                entries.sort(key=lambda r:r['rank'])
                groups.append({'name': str(name or 'Tabell'), 'items': entries})
        for child in node.get('children') or []:
            visit(child, str(name or ''))
    visit(payload)
    # ESPN returns only a stub or empty JSON in some paths; reject it explicitly.
    if not groups or sum(len(group['items']) for group in groups) < 4:
        raise ValueError('Standings endpoint returned no usable table')
    season=payload.get('season') or {}
    if isinstance(season, dict):
        season_name=season.get('displayName') or str(season.get('year') or '')
    else:
        season_name=str(season) if season else ''
    return {'season': season_name, 'groups': groups}


def update_standings(sport: str, slug: str) -> dict:
    url=f'https://site.api.espn.com/apis/v2/sports/{sport}/{slug}/standings'
    return normalize_standings(fetch_json(url), sport)

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    now = datetime.now(timezone.utc)
    # Swedish dates, including daylight saving, via standard-library zoneinfo.
    from zoneinfo import ZoneInfo
    swedish_day = now.astimezone(ZoneInfo("Europe/Stockholm")).date()
    dates = [(swedish_day + timedelta(days=delta)).strftime("%Y%m%d") for delta in (-1, 0, 1)]
    if DEST.exists():
        try:
            old = json.loads(DEST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
    else:
        old = {}
    data = {
        "schema": 1,
        "updated_at": old.get("updated_at"),
        "last_attempt": now.isoformat(timespec="seconds"),
        "leagues": old.get("leagues") or {},
        "f1": old.get("f1") or {},
        "standings": old.get("standings") or {},
        "errors": {},
    }
    refreshed = 0
    stamp = now.isoformat(timespec="seconds")
    for key, (sport, slug) in LEAGUES.items():
        try:
            matches = update_league(sport, slug, dates)
            data["leagues"][key] = {"updated_at": stamp, "matches": matches}
            refreshed += 1
            logging.info("%s: %d matches", key, len(matches))
        except Exception as exc:
            data["errors"][key] = str(exc)[:260]
    # Update current league tables independently from match feeds.
    for key, (sport, slug) in LEAGUES.items():
        try:
            table = update_standings(sport, slug)
            table['updated_at'] = stamp
            data['standings'][key] = table
            refreshed += 1
            logging.info('%s standings: %d groups', key, len(table['groups']))
        except Exception as exc:
            logging.warning('%s standings: %s', key, exc)
            data['errors'][f'standings_{key}'] = str(exc)[:260]
    for category, ending in [("drivers", "driverstandings.json"), ("constructors", "constructorstandings.json")]:
        try:
            parsed = parse_f1_standings(fetch_json(f"{F1_ROOT}/{ending}"), category)
            parsed["updated_at"] = stamp
            data["f1"][category] = parsed
            refreshed += 1
            logging.info("F1 %s: %d positions", category, len(parsed["items"]))
        except Exception as exc:
            data["errors"][f"f1_{category}"] = str(exc)[:260]
    if refreshed:
        data["updated_at"] = stamp
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logging.info("Updated: %d feeds. Issues: %d", refreshed, len(data["errors"]))
    if not refreshed:
        # Failed runs keep the previous data but are marked clearly in JSON/logs.
        raise RuntimeError("No feed updated; previous results preserved")


if __name__ == "__main__":
    main()
