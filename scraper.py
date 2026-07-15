"""
Marlboro Men's Softball - Power Rankings Scraper (v3)

Pulls live data from marlborosoftball.com and produces:
  - rankings.json               (current week's rankings, includes week_delta)
  - history/week-N.json         (snapshot for that week - for movers/trends)

New in v3:
  - Weekly snapshots to history/ folder
  - Safety guardrail: fails loudly if scrape produces 0 teams-with-games
  - Rank delta vs previous week ("week_delta") computed for main-page movers
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://marlborosoftball.com"
STANDINGS_URL = f"{BASE}/standings/"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/121.0.0.0 Safari/537.36")

ROSTER = {
    1: ("ENGAGE PEO", "POLLOCK", "East"),
    2: ("FREEHOLD BUICK GMC", "CONTI", "East"),
    3: ("PRINCETON BRAIN & SPINE", "WALLMAN", "East"),
    4: ("URWAY HEALTH", "BYKOFSKY", "East"),
    5: ("GODDARD'S HOME IMPROVEMENTS", "GODDARD", "East"),
    6: ("PROCARE REHAB", "CARROLL", "East"),
    7: ("FRADKIN LAW", "PINGARO", "Central"),
    8: ("JERSEY ALLSTARS", "BOMENBLIT", "Central"),
    9: ("ACE ALUMINUM", "ROSENSTOCK", "Central"),
    10: ("GAME CHANGER", "KESSLER", "Central"),
    11: ("SHORE SMILE", "LOMBARDI", "Central"),
    12: ("LHRGC LAW", "LAROCCA", "Central"),
    13: ("MONMOUTH GYMNASTICS", "MARRONE", "West"),
    14: ("NEW HORIZON", "MEYER", "West"),
    15: ("TEC-TEL", "DRASHINSKY", "West"),
    16: ("TUSCANYROSE", "POLZER", "West"),
    17: ("EB CONSTRUCTION", "GOLDFARB", "West"),
    18: ("CG TAX, AUDIT AND ADVISORY", "TURANO", "West"),
}

TEAM_ID_BY_DIVISION = {
    "East":    [1, 2, 3, 4, 5, 6],
    "Central": [7, 8, 9, 10, 11, 12],
    "West":    [13, 14, 15, 16, 17, 18],
}


@dataclass
class Team:
    team_id: int
    name: str
    captain: str
    division: str
    wins: int = 0
    losses: int = 0
    runs_for: int = 0
    runs_against: int = 0

    @property
    def games_played(self) -> int:
        return self.wins + self.losses

    @property
    def win_pct(self) -> float:
        gp = self.games_played
        return self.wins / gp if gp else 0.0

    @property
    def run_diff(self) -> int:
        return self.runs_for - self.runs_against

    @property
    def avg_diff(self) -> float:
        gp = self.games_played
        return self.run_diff / gp if gp else 0.0


def _fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    text = r.text
    print(f"[debug] Fetched {url}: {len(text)} chars")
    return text


def _parse_int(s: str, default: int = 0) -> int:
    s = (s or "").strip().replace("+", "").replace(",", "")
    if not s:
        return default
    m = re.match(r"-?\d+", s)
    return int(m.group()) if m else default


def _team_id_from_link(a_tag):
    if not a_tag or not a_tag.get("href"):
        return None
    m = re.search(r"[?&]id=(\d+)", a_tag["href"])
    return int(m.group(1)) if m else None


def scrape_standings() -> dict:
    """Position-agnostic, header-name-based standings parser."""
    html = _fetch(STANDINGS_URL)
    soup = BeautifulSoup(html, "html.parser")
    teams: dict = {}

    all_tables = soup.find_all("table")
    print(f"[debug] Found {len(all_tables)} tables on page")

    tables_matched = 0
    for tbl_idx, table in enumerate(all_tables):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True).lower() for c in header_cells]
        if not headers:
            continue

        def find_col(*candidates):
            for cand in candidates:
                for i, h in enumerate(headers):
                    if h == cand:
                        return i
            for cand in candidates:
                for i, h in enumerate(headers):
                    if cand in h:
                        return i
            return None

        c_w    = find_col("w")
        c_l    = find_col("l")
        c_rf   = find_col("runs for", "rf")
        c_ra   = find_col("runs against", "ra")
        c_name = find_col("team name", "team")

        if None in (c_w, c_l, c_rf, c_ra, c_name):
            continue

        tables_matched += 1
        rows_parsed = 0

        for tr in rows[1:]:
            cells = tr.find_all(["th", "td"])
            max_col = max(c_w, c_l, c_rf, c_ra, c_name)
            if len(cells) <= max_col:
                continue

            team_id = None
            for a in tr.find_all("a"):
                tid = _team_id_from_link(a)
                if tid and tid in ROSTER:
                    team_id = tid
                    break
            if not team_id:
                continue

            wins   = _parse_int(cells[c_w].get_text(strip=True))
            losses = _parse_int(cells[c_l].get_text(strip=True))
            rf     = _parse_int(cells[c_rf].get_text(strip=True))
            ra     = _parse_int(cells[c_ra].get_text(strip=True))

            roster_name, roster_capt, division = ROSTER[team_id]

            existing = teams.get(team_id)
            if existing and existing.games_played > (wins + losses):
                continue

            teams[team_id] = Team(
                team_id=team_id, name=roster_name, captain=roster_capt,
