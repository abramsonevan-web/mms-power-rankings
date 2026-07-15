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
                division=division, wins=wins, losses=losses,
                runs_for=rf, runs_against=ra,
            )
            rows_parsed += 1

        if rows_parsed:
            print(f"[debug] Table {tbl_idx} parsed {rows_parsed} rows")

    for tid, (name, capt, div) in ROSTER.items():
        if tid not in teams:
            teams[tid] = Team(team_id=tid, name=name, captain=capt, division=div)

    with_games = sum(1 for t in teams.values() if t.games_played > 0)
    print(f"[debug] Tables matched: {tables_matched}")
    print(f"[debug] Teams with games played: {with_games}/18")

    return teams


def build_dataset_live() -> dict:
    return scrape_standings()


def compute_rankings(teams: dict, week: int | None = None) -> list:
    any_games = any(t.games_played > 0 for t in teams.values())
    if not any_games:
        ranked = sorted(teams.values(), key=lambda t: t.team_id)
        rows = [_row(t, rank=i + 1, rating=0, components={}, week=0)
                for i, t in enumerate(ranked)]
        for r in rows:
            r["record_rank"] = r["rank"]
            r["rank_delta"] = 0
        return rows

    max_gp = max(t.games_played for t in teams.values())
    effective_week = week if week is not None else max_gp

    if effective_week <= 3:
        w_adjwin, w_diff, w_iter = 0.25, 0.55, 0.20
    else:
        w_adjwin, w_diff, w_iter = 0.40, 0.35, 0.25

    def opp_winpct(team: Team) -> float:
        div_teams = [t for t in teams.values()
                     if t.division == team.division and t.team_id != team.team_id]
        others = [t for t in teams.values() if t.division != team.division]
        div_avg   = sum(t.win_pct for t in div_teams) / len(div_teams) if div_teams else 0.5
        other_avg = sum(t.win_pct for t in others) / len(others) if others else 0.5
        return 0.67 * div_avg + 0.33 * other_avg

    adj_win  = {tid: 0.5 * t.win_pct + 0.5 * opp_winpct(t) for tid, t in teams.items()}
    diffs    = {tid: t.avg_diff for tid, t in teams.items()}
    max_abs  = max((abs(d) for d in diffs.values()), default=1) or 1
    diff_norm = {tid: 0.5 + 0.5 * (d / max_abs) for tid, d in diffs.items()}

    rating = {tid: t.avg_diff for tid, t in teams.items()}
    for _ in range(15):
        new_rating = {}
        for tid, t in teams.items():
            div_opps = [teams[o] for o in TEAM_ID_BY_DIVISION[t.division] if o != tid]
            opp_avg = sum(rating[o.team_id] for o in div_opps) / len(div_opps) if div_opps else 0.0
            new_rating[tid] = t.avg_diff + 0.3 * opp_avg
        rating = new_rating

    vals = list(rating.values())
    rmin, rmax = min(vals), max(vals)
    span = (rmax - rmin) or 1
    iter_norm = {tid: (r - rmin) / span for tid, r in rating.items()}

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
        results.append((t, round(score * 100, 1), components))

    results.sort(key=lambda x: -x[1])
    rows = [_row(t, rank=i + 1, rating=r, components=c, week=effective_week)
            for i, (t, r, c) in enumerate(results)]

    by_record = sorted(rows, key=lambda r: (-r["win_pct"], -r["run_diff"]))
    record_rank = {r["team_id"]: i + 1 for i, r in enumerate(by_record)}
    for r in rows:
        r["record_rank"] = record_rank[r["team_id"]]
        r["rank_delta"] = record_rank[r["team_id"]] - r["rank"]
    return rows


def _row(team: Team, rank: int, rating: float, components: dict, week: int) -> dict:
    return {
        "rank": rank, "team_id": team.team_id, "name": team.name,
        "captain": team.captain, "division": team.division,
        "wins": team.wins, "losses": team.losses,
        "win_pct": round(team.win_pct, 3),
        "runs_for": team.runs_for, "runs_against": team.runs_against,
        "run_diff": team.run_diff, "avg_diff": round(team.avg_diff, 2),
        "rating": rating, "components": components, "week": week,
    }


def enrich_with_previous_week(rankings: list, history_dir: Path) -> list:
    """Add prev_rank and week_delta by comparing to the most recent prior snapshot."""
    for r in rankings:
        r["prev_rank"] = None
        r["week_delta"] = 0

    if not history_dir.exists():
        return rankings

    current_week = rankings[0]["week"] if rankings else 0
    snapshots = sorted(history_dir.glob("week-*.json"))
    prev_snapshot = None
    for path in reversed(snapshots):
        m = re.search(r"week-(\d+)\.json$", path.name)
        if m and int(m.group(1)) < current_week:
            prev_snapshot = path
            break

    if not prev_snapshot:
        return rankings

    try:
        prev = json.loads(prev_snapshot.read_text())
        prev_rank_by_id = {row["team_id"]: row["rank"] for row in prev["rankings"]}
        for r in rankings:
            pr = prev_rank_by_id.get(r["team_id"])
            if pr is not None:
                r["prev_rank"] = pr
                r["week_delta"] = pr - r["rank"]
    except Exception as e:
        print(f"[warn] could not read {prev_snapshot}: {e}", file=sys.stderr)

    return rankings


def build_dataset_demo() -> dict:
    teams = {tid: Team(team_id=tid, name=n, captain=c, division=d)
             for tid, (n, c, d) in ROSTER.items()}
    demo = {
        17: (11, 4, 208, 119), 4:  (10, 4, 161, 110), 12: (8, 6, 158, 161),
        3:  (10, 4, 203, 114), 18: (9, 5, 161, 133),  13: (8, 6, 164, 156),
        1:  (8, 6, 163, 135),  8:  (8, 6, 121, 116),  7:  (8, 6, 149, 117),
        15: (7, 7, 151, 142),  5:  (7, 7, 142, 129),  6:  (6, 7, 115, 134),
        9:  (6, 8, 158, 177),  2:  (5, 10, 146, 197), 11: (4, 9, 99, 171),
        10: (4, 10, 130, 177), 16: (3, 10, 86, 165),  14: (3, 10, 119, 181),
    }
    for tid, (w, l, rf, ra) in demo.items():
        t = teams[tid]
        t.wins, t.losses, t.runs_for, t.runs_against = w, l, rf, ra
    return teams


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default="rankings.json")
    ap.add_argument("--history-dir", default="history")
    ap.add_argument("--allow-empty", action="store_true")
    args = ap.parse_args()

    if args.demo:
        print("Building demo data...")
        teams = build_dataset_demo()
        source = "demo"
    else:
        print("Scraping marlborosoftball.com...")
        teams = build_dataset_live()
        source = "live"

    with_games = sum(1 for t in teams.values() if t.games_played > 0)
    if source == "live" and with_games == 0 and not args.allow_empty:
        print("[ERROR] Scraped 0 teams with games played.", file=sys.stderr)
        print("[ERROR] Refusing to overwrite rankings.json with empty data.", file=sys.stderr)
        print("[ERROR] The league site's HTML likely changed. Please investigate.", file=sys.stderr)
        sys.exit(1)

    rankings = compute_rankings(teams)

    history_dir = Path(args.history_dir)
    rankings = enrich_with_previous_week(rankings, history_dir)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "week": rankings[0]["week"] if rankings else 0,
        "rankings": rankings,
    }

    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"Wrote {args.out} — {len(rankings)} teams, week {payload['week']}")

    if source == "live" and payload["week"] > 0:
        history_dir.mkdir(exist_ok=True)
        snap_path = history_dir / f"week-{payload['week']}.json"
        snap_path.write_text(json.dumps(payload, indent=2))
        print(f"Wrote snapshot to {snap_path}")


if __name__ == "__main__":
    main()
