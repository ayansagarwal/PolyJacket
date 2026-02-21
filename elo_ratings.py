"""
Elo Rating Calculator for IMLeagues Intramural Games

Score format in games_data.json: "home_score - away_score"
Elo ratings are calculated per team per sport, chronologically.
"""

import json
import re
import csv
from datetime import datetime
from collections import defaultdict

# ── Parameters ────────────────────────────────────────────────────────────────
BASE_ELO = 1000   # starting rating for every team

# Per-sport tuning.
# k_base   : base K-factor — higher = faster rating movement
# mov_weight: how much percent-margin-of-victory scales K.
#             0.0 = pure win/loss, higher = blowouts matter more.
#
# Percent margin = (winner - loser) / (winner + loser), always in [0, 1].
# This is scale-invariant, so a 2-0 cornhole win and a 60-20 basketball
# win both produce pct ≈ 1.0 and 0.5 respectively regardless of sport.
#
# Reference points for mult = 1 + weight * pct:
#   Cornhole  2-0 → pct 1.00, weight 0.25 → 1.25×  (barely rewards blowout)
#   Cornhole  2-1 → pct 0.33, weight 0.25 → 1.08×
#   Basketball 60-40 → pct 0.20, weight 1.5 → 1.30×
#   Basketball 70-30 → pct 0.40, weight 1.5 → 1.60×
#   Basketball 80-20 → pct 0.60, weight 1.5 → 1.90×
SPORT_CONFIG = {
    #  substring match on sport name (lower-case)
    'cornhole':      {'k_base': 80,  'mov_weight': 0.25},  # first to 2; margin near-irrelevant
    'dodgeball':     {'k_base': 80,  'mov_weight': 0.40},  # first to ~3; small max margin
    'basketball':    {'k_base': 100, 'mov_weight': 2.5},  # high-scoring; spread is meaningful
    'flag football': {'k_base': 100, 'mov_weight': 2.5},  # similar range to basketball
    'omegaball':     {'k_base': 100, 'mov_weight': 1.00},  # moderate default
}
DEFAULT_CONFIG = {'k_base': 100, 'mov_weight': 1.00}      # fallback for unknown sports
# ──────────────────────────────────────────────────────────────────────────────


def get_sport_config(sport: str) -> dict:
    """Return the config dict for a sport, falling back to DEFAULT_CONFIG."""
    s = sport.lower()
    for key, cfg in SPORT_CONFIG.items():
        if key in s:
            return cfg
    return DEFAULT_CONFIG


def expected_win_prob(rating_a, rating_b):
    """Expected probability that team A beats team B."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def mov_multiplier(winner_score, loser_score, weight):
    """
    Margin-of-victory multiplier using percent difference.

    pct = (winner - loser) / (winner + loser)  →  range [0, 1]

    Scale-invariant: a 2-0 cornhole win and a 60-0 basketball shutout both
    yield pct = 1.0, so the sport-specific mov_weight is the sole lever
    controlling how much blowouts matter for that sport.

    Formula: 1 + weight * pct   →  clamped to [0.5, 2.5]

    Reference points (weight=1.5):
      pct 0.10 (e.g. 55-45 basketball) → 1.15×
      pct 0.25 (e.g. 62-38 basketball) → 1.38×
      pct 0.50 (e.g. 3-1 dodgeball)    → 1.75×
      pct 1.00 (shutout)               → 2.50× (cap)
    """
    import math
    total = winner_score + loser_score
    if total == 0:
        return 1.0
    pct = (winner_score - loser_score) / total
    mult = 1.0 + weight * pct
    return max(0.5, min(2.5, mult))


def load_games(path='data/games_data.json'):
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)

    games = []
    score_re = re.compile(r'^(\d+)\s*-\s*(\d+)$')

    for g in raw:
        m = score_re.match(g['score'].strip())
        if not m:
            continue    # skip unplayed / cancelled / time-only entries

        home_pts = int(m.group(1))
        away_pts = int(m.group(2))

        try:
            date = datetime.strptime(g['date'], '%m/%d/%Y')
        except ValueError:
            continue

        games.append({
            'date':      date,
            'sport':     g['sport'],
            'home_team': g['home_team'],
            'away_team': g['away_team'],
            'home_pts':  home_pts,
            'away_pts':  away_pts,
        })

    # Chronological order is essential for Elo to be meaningful
    games.sort(key=lambda g: g['date'])
    return games


def compute_elo(games):
    """
    Walk through every game in date order and update Elo ratings.
    Returns:
        elo         – dict[sport][team] = current rating
        history     – list of per-game snapshots (pre-game ratings + outcome)
        record      – dict[sport][team] = {wins, losses, ties, games}
    """
    elo    = defaultdict(lambda: defaultdict(lambda: BASE_ELO))
    record = defaultdict(lambda: defaultdict(lambda: {'wins': 0, 'losses': 0, 'ties': 0, 'games': 0}))
    history = []

    for g in games:
        sport = g['sport']
        home  = g['home_team']
        away  = g['away_team']
        hp    = g['home_pts']
        ap    = g['away_pts']

        cfg     = get_sport_config(sport)
        k_base  = cfg['k_base']
        mov_w   = cfg['mov_weight']

        r_home = elo[sport][home]
        r_away = elo[sport][away]

        exp_home = expected_win_prob(r_home, r_away)
        exp_away = 1.0 - exp_home

        if hp > ap:
            s_home, s_away = 1.0, 0.0
            mult = mov_multiplier(hp, ap, mov_w)
        elif ap > hp:
            s_home, s_away = 0.0, 1.0
            mult = mov_multiplier(ap, hp, mov_w)
        else:
            s_home, s_away = 0.5, 0.5
            mult = 1.0

        k = k_base * mult

        new_home = r_home + k * (s_home - exp_home)
        new_away = r_away + k * (s_away - exp_away)

        history.append({
            'date':         g['date'].strftime('%m/%d/%Y'),
            'sport':        sport,
            'home_team':    home,
            'away_team':    away,
            'home_pts':     hp,
            'away_pts':     ap,
            'home_elo_pre': round(r_home, 1),
            'away_elo_pre': round(r_away, 1),
            'home_elo_post':round(new_home, 1),
            'away_elo_post':round(new_away, 1),
            'home_exp':     round(exp_home, 4),
            'away_exp':     round(exp_away, 4),
        })

        # Update ratings
        elo[sport][home] = new_home
        elo[sport][away] = new_away

        # Update records
        rec_home = record[sport][home]
        rec_away = record[sport][away]
        rec_home['games'] += 1
        rec_away['games'] += 1
        if hp > ap:
            rec_home['wins']   += 1
            rec_away['losses'] += 1
        elif ap > hp:
            rec_away['wins']   += 1
            rec_home['losses'] += 1
        else:
            rec_home['ties'] += 1
            rec_away['ties'] += 1

    return elo, history, record


def save_ratings(elo, record, path='data/elo_ratings.csv'):
    rows = []
    for sport, teams in elo.items():
        for team, rating in teams.items():
            rec = record[sport][team]
            total = rec['games']
            win_pct = (rec['wins'] + 0.5 * rec['ties']) / total if total else 0.0
            rows.append({
                'sport':    sport,
                'team':     team,
                'elo':      round(rating, 1),
                'games':    total,
                'wins':     rec['wins'],
                'losses':   rec['losses'],
                'ties':     rec['ties'],
                'win_pct':  round(win_pct, 3),
            })

    rows.sort(key=lambda r: (r['sport'], -r['elo']))

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['sport','team','elo','games','wins','losses','ties','win_pct'])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} team ratings → {path}")


def save_history(history, path='elo_history.csv'):
    if not history:
        return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=history[0].keys())
        writer.writeheader()
        writer.writerows(history)
    print(f"Saved {len(history)} game records → {path}")


def print_leaderboard(elo, record, top_n=8):
    print()
    for sport in sorted(elo.keys()):
        teams = elo[sport]
        ranked = sorted(teams.items(), key=lambda x: -x[1])
        print(f"{'─'*62}")
        print(f"  {sport}")
        print(f"{'─'*62}")
        print(f"  {'Rank':<5} {'Team':<30} {'Elo':>6}  {'W-L-T':<10} {'Win%':>5}")
        print(f"  {'----':<5} {'----':<30} {'---':>6}  {'-----':<10} {'----':>5}")
        for rank, (team, rating) in enumerate(ranked[:top_n], 1):
            rec = record[sport][team]
            wlt = f"{rec['wins']}-{rec['losses']}-{rec['ties']}"
            total = rec['games']
            win_pct = (rec['wins'] + 0.5 * rec['ties']) / total if total else 0.0
            print(f"  {rank:<5} {team:<30} {rating:>6.1f}  {wlt:<10} {win_pct:>4.0%}")
        print()


def predict_matchup(elo, sport, away_team, home_team):
    """Quick prediction helper using final Elo ratings."""
    r_away = elo[sport].get(away_team, BASE_ELO)
    r_home = elo[sport].get(home_team, BASE_ELO)
    p_away = expected_win_prob(r_away, r_home)
    p_home = 1.0 - p_away
    print(f"\n  Matchup: {away_team} (away) vs {home_team} (home) [{sport}]")
    print(f"  Elo:     {r_away:.1f}  vs  {r_home:.1f}")
    print(f"  Win%:    {p_away:.1%}  vs  {p_home:.1%}")
    return p_away, p_home


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Loading games...")
    games = load_games()
    print(f"  {len(games)} scored games across "
          f"{len({g['sport'] for g in games})} sports, "
          f"{len({g['home_team'] for g in games} | {g['away_team'] for g in games})} teams")

    print("\nCalculating Elo ratings...")
    elo, history, record = compute_elo(games)

    save_ratings(elo, record)
    save_history(history)

    print_leaderboard(elo, record)

    # --- Example predictions ---
    print("Example predictions")
    print("=" * 62)
    predict_matchup(elo, '5v5 Basketball', 'Om Patel’s honourable team', 'Lebum')
    predict_matchup(elo, '4v4 Flag Football', 'Haynes “Haynes King” King', 'Tuna Sub')
    predict_matchup(elo, 'Cornhole', 'Cornballs', 'TKE A')
