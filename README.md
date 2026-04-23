# MMS Power Index

A weekly power ranking tool for the Marlboro Men's Softball League that ranks all 18 teams 1-18 using record, run differential, and strength of schedule — so a losing team with a brutal schedule doesn't look as bad as its record, and a winning team that's feasted on the bottom of the league doesn't look as good.

## What's in this package

```
index.html               ← Standalone rankings page. Open in any browser.
scraper.py               ← Pulls live data from marlborosoftball.com.
rankings.json            ← Output of scraper — what the page reads when live.
demo_rankings.json       ← Hand-crafted Week 6 example data (used to seed index.html).
github_workflow_weekly.yml  ← Automation — runs scraper every Monday morning.
```

## How the model works

The Power Index combines three components into a single 0-100 rating:

| Component | Weight (normal) | Weight (weeks 1-3) | What it captures |
|---|---|---|---|
| Adjusted Win % (RPI-style) | 40% | 25% | Your record + your opponents' records |
| Average Run Differential | 35% | 55% | How decisively you win/lose — no blowout cap |
| Iterative Opp-Adjusted Rating | 25% | 20% | Massey-style: solves for true strength over 25 passes |

Early in the season, we lean harder on run diff because SOS hasn't had time to stabilize.

The page also shows **record rank vs power rank** for every team so "underrated" and "overrated" teams jump out automatically. URWAY HEALTH at 0-9 ranked #12 because they've played the league's hardest schedule, or FRADKIN LAW at 6-0 dropping to #4 because they've mostly played the Central's cellar — those are the stories the podcast will chew on all week.

## Running locally

```bash
pip install requests beautifulsoup4

python scraper.py --demo               # Build demo data (mid-season Week 6 sim)
python scraper.py                      # Live scrape from marlborosoftball.com
python scraper.py --out rankings.json  # Custom output path
```

Then just open `index.html` in a browser.

## Deploying — the "standalone link I can share" path

**Recommended: GitHub Pages (free, automatic weekly updates)**

1. Create a new GitHub repo, push all these files to the root.
2. Move `github_workflow_weekly.yml` to `.github/workflows/weekly.yml`.
3. In the repo settings, turn on GitHub Pages from the main branch.
4. Share the URL — e.g., `https://yourname.github.io/mms-power-index/`.

Every Monday at 9 AM Eastern, the Action scrapes the league site, regenerates `rankings.json`, and the page automatically picks it up.

**Alternative: Netlify or Vercel**

Both have free tiers and the same GitHub-to-deploy workflow. Same `.yml` file works.

**Dead simple: just host the HTML**

`index.html` has the current week's data embedded in it. You can email it, put it in a Dropbox public link, or hand it to J Farell Media to drop into the WordPress site as a single page. The page will try to load a newer `rankings.json` from the same directory if it exists, but it'll always fall back to the embedded snapshot, so it never breaks.

## Scraper notes

The standings page is plain HTML and parses cleanly. Individual team pages contain the per-game results — the scraper uses a defensive heuristic that looks for rows with an opponent link + two numeric scores, so minor template tweaks on the website shouldn't break it.

If the page layout changes and the scraper misreads something, open `scraper.py` and look at `scrape_standings()` and `scrape_team_games()` — both are clearly commented and should be easy to adjust.

## Customizing

- **Change the component weights** — edit `w_adjwin`, `w_diff`, `w_iter` at the top of `compute_rankings()` in `scraper.py`.
- **Add a blowout cap** — in the `Game` class, change the `margin` property to `return max(-N, min(N, self.runs_for - self.runs_against))`.
- **Change the run schedule** — edit the cron in `github_workflow_weekly.yml`. Current setting: Mondays 9 AM ET.
- **Change colors/fonts** — CSS variables at the top of `index.html`.

## Roadmap ideas

- Movers (rank vs. last week) — needs to persist previous week's rankings.json
- Projected playoff seeding if the season ended today
- Head-to-head "who beats who" grid using the iterative ratings
- Weekly "game of the week" based on two highest-ranked opponents playing
