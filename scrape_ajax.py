"""
IMLeagues Game Scraper - AJAX Endpoint Version

Uses the AJAX endpoint called when "Entire Season" is selected to get clean JSON.
Captures the POST request via Selenium, then replays it with requests.
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import time
import json
import csv
import requests

AJAX_URL = "https://www.imleagues.com/AjaxPageRequestHandler.aspx?class=imLeagues.Web.Members.Pages.BO.School.ManageGamesBO&method=AjaxSearchGamesForSPAManageGames"

def capture_ajax_request():
    """
    Open the page, select Entire Season, and capture the AJAX POST params + cookies.
    Returns (cookies_dict, post_body_str).
    """
    options = webdriver.ChromeOptions()
    # Enable Chrome DevTools Protocol for network capture
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.add_argument('--log-level=3')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(10)
    wait = WebDriverWait(driver, 20)

    post_body = None
    cookies_dict = {}

    try:
        print("Loading manage games page...")
        driver.get("https://www.imleagues.com/spa/intramural/13cc30785f6f4658aebbb07d83e19f67/managegames")
        time.sleep(5)

        # Inject XHR intercept before triggering the dropdown
        print("Injecting XHR interceptor...")
        driver.execute_script("""
            window._capturedPostBody = null;
            var origOpen = XMLHttpRequest.prototype.open;
            var origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this._url = url;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function(body) {
                if (this._url && this._url.includes('AjaxSearchGamesForSPAManageGames')) {
                    window._capturedPostBody = body;
                    console.log('Captured POST body:', body);
                }
                return origSend.apply(this, arguments);
            };
        """)

        # Click "Entire Season" via JavaScript
        print("Selecting 'Entire Season'...")
        result = driver.execute_script("""
            var options = document.querySelectorAll('option');
            for (var i = 0; i < options.length; i++) {
                if (options[i].textContent.toLowerCase().includes('entire season')) {
                    options[i].selected = true;
                    var event = new Event('change', { bubbles: true });
                    options[i].parentElement.dispatchEvent(event);
                    return options[i].textContent.trim();
                }
            }
            return null;
        """)
        print(f"  Selected option: {result}")

        # Wait for AJAX to fire and be captured
        for _ in range(20):
            post_body = driver.execute_script("return window._capturedPostBody;")
            if post_body:
                print(f"  Captured POST body: {post_body[:200]}")
                break
            time.sleep(0.5)

        if not post_body:
            print("  XHR interceptor missed it, trying performance logs...")
            logs = driver.get_log("performance")
            for entry in logs:
                msg = json.loads(entry["message"])["message"]
                if msg.get("method") == "Network.requestWillBeSent":
                    req = msg["params"].get("request", {})
                    if "AjaxSearchGamesForSPAManageGames" in req.get("url", ""):
                        post_body = req.get("postData", "")
                        print(f"  Found via perf log: {post_body[:200]}")
                        break

        # Grab session cookies
        for cookie in driver.get_cookies():
            cookies_dict[cookie["name"]] = cookie["value"]

        print(f"  Captured {len(cookies_dict)} cookies")

    finally:
        driver.quit()

    return cookies_dict, post_body


def fetch_all_games(cookies_dict, post_body):
    """POST to the AJAX endpoint with the captured cookies and body."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.imleagues.com/spa/intramural/13cc30785f6f4658aebbb07d83e19f67/managegames",
        "User-Agent": "Mozilla/5.0",
    }
    print(f"\nPOSTing to AJAX endpoint...")
    resp = requests.post(AJAX_URL, data=post_body, headers=headers, cookies=cookies_dict, timeout=60)
    resp.raise_for_status()
    return resp.json()


def clean_team_name(title_attr):
    """Strip any HTML tags from the title attribute value."""
    if not title_attr:
        return "Unknown"
    if "<" in title_attr:
        inner = BeautifulSoup(title_attr, "html.parser")
        return inner.get_text(strip=True)
    return title_attr.strip()


def parse_games(data):
    """Parse the AJAX HTML response into a flat list of game dicts."""
    html = data.get("Data", "")
    if not html:
        print("ERROR: 'Data' key missing or empty in response.")
        return []

    soup = BeautifulSoup(html, "html.parser")

    games = []
    skipped = 0

    # Each GameTypeRow has the date in its 'gameday' attribute and contains
    # self-contained game divs — no cross-column contamination possible.
    for type_row in soup.find_all("div", class_="GameTypeRow"):
        raw_date = type_row.get("gameday", "").strip()
        # raw_date is MM/DD/YYYY; convert to M/D/YYYY
        if raw_date and raw_date != "01/01/1900":
            try:
                m, d, y = raw_date.split("/")
                date_str = f"{int(m)}/{int(d)}/{y}"
            except ValueError:
                date_str = raw_date
        else:
            date_str = "Unknown"

        for game_div in type_row.find_all("div", class_="iml-game-list"):
            # Sport: first breadcrumb link that goes to /spa/sport/
            sport = "Unknown"
            for a in game_div.find_all("a", href=True):
                if "/spa/sport/" in a["href"]:
                    sport = a.get_text(strip=True)
                    break

            # Home + away team names from title attribute
            home_tag = game_div.find("a", {"aria-label": "Home Team"})
            away_tag = game_div.find("a", {"aria-label": "Away Team"})
            home_team = clean_team_name(home_tag.get("title")) if home_tag else "Unknown"
            away_team = clean_team_name(away_tag.get("title")) if away_tag else "Unknown"

            # Score: separate left/right score elements
            score1_tag = game_div.find("strong", class_="match-team1Score")
            score2_tag = game_div.find("strong", class_="match-team2Score")
            score1 = score1_tag.get_text(strip=True) if score1_tag else ""
            score2 = score2_tag.get_text(strip=True) if score2_tag else ""

            if score1 and score2 and score1 != "--" and score2 != "--":
                score = f"{score1} - {score2}"
            else:
                # Fall back to the h5 status span (time or FINAL)
                status_tag = game_div.find("span", class_="status")
                score = status_tag.get_text(strip=True) if status_tag else "Unknown"

            # Skip BYE games and TBD/unscheduled placeholders
            if (home_team.upper() in ("BYE", "TBD") or
                    away_team.upper() in ("BYE", "TBD") or
                    date_str == "Unknown"):
                skipped += 1
                continue

            games.append({
                "date":      date_str,
                "sport":     sport,
                "away_team": away_team,
                "home_team": home_team,
                "score":     score,
            })

    print(f"Parsed {len(games)} games (skipped {skipped} BYE games)")
    return games


def main():
    print("=" * 60)
    print("IMLeagues AJAX Game Scraper")
    print("=" * 60)

    # Step 1: capture the POST body and session cookies
    cookies, post_body = capture_ajax_request()

    if not post_body:
        print("\nERROR: Could not capture POST body. Dumping cookies and aborting.")
        print("Cookies:", cookies)
        return

    # Step 2: replay the request
    raw = fetch_all_games(cookies, post_body)

    # Step 3: save the raw response for inspection
    with open("ajax_raw.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    print("Saved raw response to ajax_raw.json")

    # Step 4: parse into flat game list
    games = parse_games(raw)

    if not games:
        print("\nNo games parsed. Check ajax_raw.json to inspect the structure.")
        return

    # Step 5: save results
    with open("data/games_data.json", "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, ensure_ascii=False)

    with open("data/games_data.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "sport", "away_team", "home_team", "score"])
        writer.writeheader()
        writer.writerows(games)

    print(f"\n✓ Saved {len(games)} games to games_data.json and games_data.csv")

    # Summary
    sports = {}
    for g in games:
        sports[g["sport"]] = sports.get(g["sport"], 0) + 1
    print("\nGames by sport:")
    for s in sorted(sports):
        print(f"  {s}: {sports[s]}")

    print("\nSample (first 3):")
    for g in games[:3]:
        print(f"  {g['date']} | {g['sport']} | {g['away_team']} vs {g['home_team']} | {g['score']}")


if __name__ == "__main__":
    main()
