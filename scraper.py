"""
Marlboro Men's Softball - Power Rankings Scraper

Pulls live data from marlborosoftball.com and produces rankings.json
for the power rankings page to consume.

Usage:
    python scraper.py                    # scrape live, write rankings.json
    python scraper.py --demo             # write demo rankings.json (mid-season sim)
    python scraper.py --out path.json    # custom output path

Run weekly via GitHub Actions (see .github/workflows/weekly.yml).
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://marlborosoftball.com"
STANDINGS_URL = f"{BASE}/standings/"
TEAM_PAGE_URL = f"{BASE}/team-page/?id={{team_id}}"
USER_AGENT = "MarlboroPowerRankings/1.0 (league tool; contact league commissioner)"

DIVISIONS = {
    "East":    [1, 2, 3, 4, 5, 6],
    "Central": [7, 8, 9, 10, 11, 12],
    "West":    [13, 14, 15, 16, 17, 18],
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Game:
    team_id: int
    opponent_id: int
    runs_for: int
    runs_against: int
    week: int = 0

    @property
    def won(self) -> bool:
        return self.runs_for > self.runs_against

    @property
    def margin(self) -> int:
        return self.runs_for - self.runs_against


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
    games: list = field(default_factory=list)

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


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def _fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def scrape_standings() -> dict:
    """Read the league standings page. Returns {team_id: Team}.

    The site has THREE separate tables (one per division), each preceded
    by an <h3> with the division name. We find the division headers, then
    take the next standings-style table after each.
    """
    html = _fetch(STANDINGS_URL)
    soup = BeautifulSoup(html, "html.parser")
    teams: dict = {}

    def parse_int(s: str, default: int = 0) -> int:
        s = (s or "").strip().replace("+", "")
        if not s:
            return default
        try:
            return int(float(s))
        except ValueError:
            return default

    def row_cells(tr):
        """Return all cells in a row, whether they're <th> or <td>."""
        return tr.find_all(["th", "td"])

    def header_text(tr):
        """Get lowercase text of every cell in a row."""
        return [c.get_text(strip=True).lower() for c in row_cells(tr)]

    def is_standings_header(headers):
        """Recognize a standings table header row."""
        return ("team name" in headers and "runs for" in headers
                and ("w" in headers) and ("l" in headers))

    parsed_divisions = 0
    for h3 in soup.find_all(["h3", "h2"]):
        text = h3.get_text(strip=True)
        division = None
        for d in ("East", "Central", "West"):
            if d.lower() in text.lower() and "division" in text.lower():
                division = d
                break
        if not division:
            continue

        # Walk forward to the next table whose first row looks like a standings header
        table = h3.find_next("table")
        target_table = None
        target_header_row = None
        while table is not None:
            for tr in table.find_all("tr"):
                headers = header_text(tr)
                if is_standings_header(headers):
                    target_table = table
                    target_header_row = tr
                    break
            if target_table is not None:
                break
            table = table.find_next("table")

        if target_table is None:
            print(f"[warn] no standings table found for {division} division",
                  file=sys.stderr)
            continue

        # Build a header -> column-index map
        headers = header_text(target_header_row)
        col = {name: i for i, name in enumerate(headers)}

        required = ["team #", "team name", "w", "l", "runs for", "runs against"]
        missing = [c for c in required if c not in col]
        if missing:
            print(f"[warn] {division}: missing columns {missing} in {headers}",
                  file=sys.stderr)
            continue

        # Captain column may be "capt." or "capt" depending on rendering
        capt_idx = col.get("capt.") or col.get("capt") or 1

        # Parse data rows (skip the header row itself)
        rows_parsed = 0
        for tr in target_table.find_all("tr"):
            if tr is target_header_row:
                continue
            cells = row_cells(tr)
            if len(cells) < len(headers):
                continue
            team_id = parse_int(cells[col["team #"]].get_text(strip=True))
            if team_id == 0:
                continue
            teams[team_id] = Team(
                team_id=team_id,
                name=cells[col["team name"]].get_text(strip=True),
                captain=cells[capt_idx].get_text(strip=True),
                division=division,
                wins=parse_int(cells[col["w"]].get_text(strip=True)),
                losses=parse_int(cells[col["l"]].get_text(strip=True)),
                runs_for=parse_int(cells[col["runs for"]].get_text(strip=True)),
                runs_against=parse_int(cells[col["runs against"]].get_text(strip=True)),
            )
            rows_parsed += 1
        print(f"[debug] {division}: parsed {rows_parsed} teams")
        parsed_divisions += 1

    print(f"[debug] Total: {parsed_divisions} divisions, {len(teams)} teams")
    return teams


def scrape_team_games(team_id: int) -> list:
    """Pull completed games from a team page. Returns list of Game.

    The team pages list each game with opponent and final score. Exact markup
    may vary — this function is written defensively and will return whatever
    it can parse. If the season hasn't started, it returns []."""
    html = _fetch(TEAM_PAGE_URL.format(team_id=team_id))
    soup = BeautifulSoup(html, "html.parser")
    games = []

    # Heuristic: look for rows that contain an opponent team-page link and two numeric scores.
    score_re = re.compile(r"^\s*(\d{1,2})\s*$")
    for row in soup.find_all(["tr", "div", "li"]):
        text = row.get_text(" ", strip=True)
        link = row.find("a", href=re.compile(r"team-page.*id=\d+"))
        if not link:
            continue
        opp_match = re.search(r"id=(\d+)", link["href"])
        if not opp_match:
            continue
        opp_id = int(opp_match.group(1))
        if opp_id == team_id:
            continue
        # Find two small integers that look like a final score
        nums = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)]
        if len(nums) < 2:
            continue
        # Take the last two numbers as the score — matches most templates
        rf, ra = nums[-2], nums[-1]
        games.append(Game(team_id=team_id, opponent_id=opp_id,
                          runs_for=rf, runs_against=ra))
    return games


def build_dataset_live() -> dict:
    teams = scrape_standings()

    # Ensure all 18 teams exist even if some haven't played yet
    # (the standings table may omit teams with 0-0 records on some weeks)
    roster_fallback = {
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
    for tid, (name, capt, div) in roster_fallback.items():
        if tid not in teams:
            teams[tid] = Team(team_id=tid, name=name, captain=capt, division=div)

    # Try to pull individual game results from each team page
    for tid, team in teams.items():
        if team.games_played == 0:
            continue  # no games played yet, nothing to fetch
        try:
            team.games = scrape_team_games(tid)
        except Exception as e:
            print(f"[warn] could not parse team {tid} games: {e}", file=sys.stderr)

    # Fallback: if team-page parsing yielded nothing for a team that has played,
    # synthesize game records from the season totals so SOS can still be computed.
    # We don't know exact opponents, but we can at least count wins/losses with
    # average runs scored/allowed per game, and the iterative rating will degrade
    # gracefully (it just contributes less signal).
    for tid, team in teams.items():
        if team.games or team.games_played == 0:
            continue
        # Without per-game data we can't link to opponents, but we can still
        # compute Adjusted Win % and Run Differential — those don't need it.
        # Mark with empty games list; the model handles this.
        pass

    return teams


# ---------------------------------------------------------------------------
# Ranking model
# ---------------------------------------------------------------------------
def compute_rankings(teams: dict, week: int | None = None) -> list:
    """Compute power ratings. Returns a list of dicts, ranked 1 -> 18.

    Model components:
      1. Adjusted Win % (40%) - win % plus opponents' avg win % (RPI-style SOS)
      2. Avg Run Diff    (35%) - normalized to 0-1
      3. Iterative Rating (25%) - Massey-style opponent-adjusted margin

    Early-season (weeks 1-3) shifts weight away from SOS toward run diff.
    """
    any_games = any(t.games_played > 0 for t in teams.values())
    if not any_games:
        # Preseason — return teams ranked by team number with zero rating
        ranked = sorted(teams.values(), key=lambda t: t.team_id)
        return [_row(t, rank=i + 1, rating=0, components={}, week=0)
                for i, t in enumerate(ranked)]

    max_gp = max(t.games_played for t in teams.values())
    effective_week = week if week is not None else max_gp

    # --- Early-season weight adjustment ---
    if effective_week <= 3:
        w_adjwin, w_diff, w_iter = 0.25, 0.55, 0.20
    else:
        w_adjwin, w_diff, w_iter = 0.40, 0.35, 0.25

    # --- 1. Adjusted Win % (RPI-style) ---
    # = 0.5 * team_winpct + 0.5 * avg opponent winpct
    def opp_winpct(team: Team) -> float:
        opps = [teams[g.opponent_id] for g in team.games if g.opponent_id in teams]
        if not opps:
            return 0.5
        return sum(o.win_pct for o in opps) / len(opps)

    adj_win = {tid: 0.5 * t.win_pct + 0.5 * opp_winpct(t)
               for tid, t in teams.items()}

    # --- 2. Avg Run Diff (normalized) ---
    diffs = {tid: t.avg_diff for tid, t in teams.items()}
    max_abs_diff = max((abs(d) for d in diffs.values()), default=1) or 1
    diff_norm = {tid: 0.5 + 0.5 * (d / max_abs_diff) for tid, d in diffs.items()}

    # --- 3. Iterative rating (Massey-lite) ---
    # rating_i = avg over games g of (margin_g + rating_opp) — scaled
    rating = {tid: 0.0 for tid in teams}
    for _ in range(25):  # converges quickly for 18 teams
        new_rating = {}
        for tid, t in teams.items():
            if not t.games:
                new_rating[tid] = 0.0
                continue
            total = sum(g.margin + rating.get(g.opponent_id, 0.0) for g in t.games)
            new_rating[tid] = total / len(t.games)
        rating = new_rating
    # Normalize iterative rating to 0-1
    vals = list(rating.values())
    rmin, rmax = min(vals), max(vals)
    span = (rmax - rmin) or 1
    iter_norm = {tid: (r - rmin) / span for tid, r in rating.items()}

    # --- Combine ---
    results = []
    for tid, t in teams.items():
        components = {
            "adj_win_pct": round(adj_win[tid], 4),
            "avg_run_diff": round(diffs[tid], 3),
            "iter_rating": round(rating[tid], 3),
            "opp_win_pct": round(opp_winpct(t), 4),
        }
        score = (w_adjwin * adj_win[tid]
                 + w_diff * diff_norm[tid]
                 + w_iter * iter_norm[tid])
        # Scale to 0-100 for display
        rating_display = round(score * 100, 1)
        results.append((t, rating_display, components))

    results.sort(key=lambda x: -x[1])
    rows = [_row(t, rank=i + 1, rating=r, components=c, week=effective_week)
            for i, (t, r, c) in enumerate(results)]

    # Expected "record rank" = rank by win % then run diff
    by_record = sorted(rows, key=lambda r: (-r["win_pct"], -r["run_diff"]))
    record_rank = {r["team_id"]: i + 1 for i, r in enumerate(by_record)}
    for r in rows:
        r["record_rank"] = record_rank[r["team_id"]]
        # Positive delta = underrated (power rank better than record rank would suggest)
        r["rank_delta"] = record_rank[r["team_id"]] - r["rank"]
    return rows


def _row(team: Team, rank: int, rating: float,
         components: dict, week: int) -> dict:
    return {
        "rank": rank,
        "team_id": team.team_id,
        "name": team.name,
        "captain": team.captain,
        "division": team.division,
        "wins": team.wins,
        "losses": team.losses,
        "win_pct": round(team.win_pct, 3),
        "runs_for": team.runs_for,
        "runs_against": team.runs_against,
        "run_diff": team.run_diff,
        "avg_diff": round(team.avg_diff, 2),
        "rating": rating,
        "components": components,
        "week": week,
    }


# ---------------------------------------------------------------------------
# Demo data — realistic mid-season (Week 6) simulation
# ---------------------------------------------------------------------------
def build_dataset_demo() -> dict:
    """Simulates a Week 6 league state with strong narrative hooks:
       - ENGAGE PEO (#1) is great and plays in a loaded East
       - URWAY HEALTH (#4) is 2-4 but has the hardest schedule in the league
       - ACE ALUMINUM (#9) is 5-1 but against weak competition
       - EB CONSTRUCTION (#17) is surprisingly good
    """
    roster = {
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

    teams = {tid: Team(team_id=tid, name=n, captain=c, division=d)
             for tid, (n, c, d) in roster.items()}

    # Hand-crafted Week 6 games (home_id, away_id, home_runs, away_runs)
    # Designed to surface narratives:
    #  - URWAY (#4) plays a brutal schedule — goes 0-7 but competitive
    #  - ACE (#9) blowout-wins vs bottom of Central — looks elite, is fraudulent
    #  - EB CONSTRUCTION (#17) is the breakout story of the West
    #  - ENGAGE PEO (#1) is the clear best team in the league
    games = [
        # --- East (loaded division, ENGAGE PEO dominant, URWAY brutalized by schedule)
        (1, 2, 14, 11), (1, 3, 18, 9),  (1, 4, 13, 12), (1, 5, 16, 8),  (1, 6, 20, 6),
        (2, 3, 12, 11), (2, 4, 11, 9),  (2, 5, 14, 7),  (2, 6, 16, 8),
        (3, 4, 13, 11), (3, 5, 12, 8),  (3, 6, 17, 9),
        (4, 5, 11, 13), (4, 6, 9, 10),  # URWAY loses close to the weak East teams too
        (5, 6, 12, 9),
        # --- Central (FRADKIN legit elite; ACE beats up on cupcakes)
        (7, 8, 14, 9),  (7, 10, 18, 7), (7, 11, 16, 8), (7, 12, 15, 6), (7, 9, 13, 10),
        (8, 10, 12, 9), (8, 11, 11, 10),
        (9, 10, 19, 4), (9, 11, 21, 6), (9, 12, 18, 3), (9, 8, 13, 9),  # ACE feasts
        (10, 11, 10, 9), (10, 12, 12, 8),
        (11, 12, 11, 10),
        # --- West (EB CONSTRUCTION breakout, TUSCANY & CG TAX cellar)
        (17, 13, 14, 9),  (17, 14, 13, 10), (17, 15, 15, 11), (17, 16, 18, 7), (17, 18, 17, 6),
        (13, 14, 12, 8),  (13, 15, 14, 10), (13, 16, 16, 9),
        (14, 15, 11, 10), (14, 16, 13, 10),
        (15, 16, 12, 11), (15, 18, 14, 8),
        (16, 18, 10, 9),
        # --- Cross-division (crucial: URWAY plays tough non-division too)
        (4, 17, 10, 12),  # URWAY loses close to EB
        (4, 7, 9, 11),    # URWAY loses close to FRADKIN
        (4, 9, 8, 10),    # URWAY loses close to ACE (ACE's only quality win)
        (18, 4, 8, 7),    # URWAY even loses a one-run game to CG TAX (brutal stretch)
    ]

    for home_id, away_id, hr, ar in games:
        home = teams[home_id]
        away = teams[away_id]
        home.games.append(Game(home_id, away_id, hr, ar))
        away.games.append(Game(away_id, home_id, ar, hr))
        home.runs_for += hr
        home.runs_against += ar
        away.runs_for += ar
        away.runs_against += hr
        if hr > ar:
            home.wins += 1
            away.losses += 1
        else:
            away.wins += 1
            home.losses += 1

    return teams


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="Use hand-crafted Week 6 demo data")
    ap.add_argument("--out", default="rankings.json",
                    help="Output JSON path")
    args = ap.parse_args()

    if args.demo:
        print("Building demo (Week 6 simulation)...")
        teams = build_dataset_demo()
    else:
        print("Scraping marlborosoftball.com...")
        teams = build_dataset_live()

    rankings = compute_rankings(teams)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "demo" if args.demo else "live",
        "week": rankings[0]["week"] if rankings else 0,
        "rankings": rankings,
    }

    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"Wrote {args.out} — {len(rankings)} teams, week {payload['week']}")


if __name__ == "__main__":
    main()
