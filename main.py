"""
FastAPI Backend for Georgia Tech IM Prediction Market
Fetches game data from IMLeagues API endpoint
"""

from fastapi import FastAPI, HTTPException, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import httpx
from bs4 import BeautifulSoup
from typing import List, Optional, Dict
from pydantic import BaseModel
import os
import json
from pathlib import Path
from datetime import datetime, timedelta
import uuid
import math

app = FastAPI(title="GT IM Prediction Market API")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For hackathon - be more restrictive in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (frontend)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Cache file for development
CACHE_FILE = Path("games_cache.json")

# In-memory storage (use database in production)
users: Dict[str, dict] = {}
markets: Dict[str, dict] = {}
user_positions: Dict[str, dict] = {}  # user_id -> {market_id: {home_shares, away_shares}}
games_data = []  # Cached games loaded on startup (List[Game])

# Constants
STARTING_BALANCE = 10000  # Starting tokens for new users
LIQUIDITY_PARAMETER = 100  # For AMM pricing (b parameter in LMSR)


# ============== MODELS ==============

class Game(BaseModel):
    """Game data model"""
    game_id: str
    home_team: str
    away_team: str
    home_score: str
    away_score: str
    time: str
    date: Optional[str] = None
    sport: str
    status: str
    location: Optional[str] = None
    league: Optional[str] = None
    home_record: Optional[str] = None
    away_record: Optional[str] = None


class GamesResponse(BaseModel):
    """API response model"""
    success: bool
    total_games: int
    games: List[Game]
    message: Optional[str] = None


class Market(BaseModel):
    """Prediction market for a game"""
    market_id: str
    game_id: str
    home_team: str
    away_team: str
    sport: str
    game_time: str
    game_date: str
    status: str  # 'open', 'closed', 'settled'
    home_price: float  # Current price for home team win (0-100)
    away_price: float  # Current price for away team win (0-100)
    home_shares: float  # Total shares for home team
    away_shares: float  # Total shares for away team
    total_volume: float  # Total tokens traded
    winner: Optional[str] = None  # 'home', 'away', or None
    home_score: Optional[str] = None  # Final score for home team
    away_score: Optional[str] = None  # Final score for away team


class Position(BaseModel):
    """User's position in a market"""
    market_id: str
    game: str
    home_shares: float
    away_shares: float
    avg_home_price: float
    avg_away_price: float
    current_value: float
    potential_return: float


class Portfolio(BaseModel):
    """User's portfolio"""
    user_id: str
    balance: float
    total_value: float
    open_positions: List[Position]
    settled_positions: List[Position]


class TradeRequest(BaseModel):
    """Request to buy shares"""
    market_id: str
    outcome: str  # 'home' or 'away'
    amount: float  # Amount in tokens to spend


class TradeResponse(BaseModel):
    """Response from trade execution"""
    success: bool
    shares_purchased: float
    price_per_share: float
    total_cost: float
    new_balance: float
    new_position: Position
    message: str


class MarketsResponse(BaseModel):
    """Response with all markets"""
    success: bool
    total_markets: int
    open_markets: int
    closed_markets: int
    settled_markets: int
    markets: List[Market]


# ============== UTILITY FUNCTIONS ==============

def get_or_create_user(user_id: Optional[str] = None) -> str:
    """Get existing user or create new one"""
    if not user_id or user_id not in users:
        user_id = str(uuid.uuid4())
        users[user_id] = {
            "balance": STARTING_BALANCE,
            "created_at": datetime.now().isoformat()
        }
        user_positions[user_id] = {}
    return user_id


def calculate_lmsr_price(shares_yes: float, shares_no: float, b: float = LIQUIDITY_PARAMETER) -> tuple:
    """
    Calculate prices using Logarithmic Market Scoring Rule (LMSR)
    Returns (price_yes, price_no) as probabilities (0-100)
    """
    try:
        exp_yes = math.exp(shares_yes / b)
        exp_no = math.exp(shares_no / b)
        total = exp_yes + exp_no
        price_yes = (exp_yes / total) * 100
        price_no = (exp_no / total) * 100
        return (price_yes, price_no)
    except:
        return (50.0, 50.0)


def calculate_cost(current_shares: float, new_shares: float, other_shares: float, b: float = LIQUIDITY_PARAMETER) -> float:
    """
    Calculate cost to buy shares using LMSR
    """
    try:
        before = b * math.log(math.exp(current_shares / b) + math.exp(other_shares / b))
        after = b * math.log(math.exp(new_shares / b) + math.exp(other_shares / b))
        return max(0, after - before)
    except:
        return 0


def is_market_closed(game_time: str, game_date: str) -> bool:
    """Check if market should be closed based on game time"""
    try:
        # Parse the game date and time
        game_datetime_str = f"{game_date} {game_time}"
        # Try multiple formats
        for fmt in ["%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y TBD"]:
            try:
                game_datetime = datetime.strptime(game_datetime_str, fmt)
                # Market closes at game start time
                return datetime.now() >= game_datetime
            except:
                continue
        # If time is TBD, keep market open
        if game_time == "TBD":
            return False
        return False
    except:
        return False


def create_markets_from_games(games: List[Game]):
    """Create or update markets from game data"""
    for game in games:
        market_id = f"market_{game.game_id}"
        
        # Determine market status
        if game.status in ['completed', 'forfeit']:
            status = 'settled'
            winner = None
            if game.home_score != '--' and game.away_score != '--':
                try:
                    home_score_int = int(game.home_score)
                    away_score_int = int(game.away_score)
                    winner = 'home' if home_score_int > away_score_int else 'away'
                except:
                    pass
        elif is_market_closed(game.time, game.date or ""):
            status = 'closed'
            winner = None
        else:
            status = 'open'
            winner = None
        
        # Create or update market
        if market_id not in markets:
            markets[market_id] = {
                "market_id": market_id,
                "game_id": game.game_id,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "sport": game.sport,
                "game_time": game.time,
                "game_date": game.date or "",
                "status": status,
                "home_shares": 0.0,
                "away_shares": 0.0,
                "total_volume": 0.0,
                "winner": winner,
                "home_score": game.home_score,
                "away_score": game.away_score
            }
        else:
            # Update status, winner, and scores
            markets[market_id]["status"] = status
            markets[market_id]["winner"] = winner
            markets[market_id]["home_score"] = game.home_score
            markets[market_id]["away_score"] = game.away_score
        
        # Calculate current prices
        home_price, away_price = calculate_lmsr_price(
            markets[market_id]["home_shares"],
            markets[market_id]["away_shares"]
        )
        markets[market_id]["home_price"] = round(home_price, 2)
        markets[market_id]["away_price"] = round(away_price, 2)


def get_user_portfolio(user_id: str) -> Portfolio:
    """Get user's complete portfolio"""
    if user_id not in users:
        return Portfolio(
            user_id=user_id,
            balance=0,
            total_value=0,
            open_positions=[],
            settled_positions=[]
        )
    
    balance = users[user_id]["balance"]
    open_positions = []
    settled_positions = []
    
    positions = user_positions.get(user_id, {})
    
    for market_id, pos in positions.items():
        if market_id not in markets:
            continue
            
        market = markets[market_id]
        home_shares = pos.get("home_shares", 0)
        away_shares = pos.get("away_shares", 0)
        
        if home_shares == 0 and away_shares == 0:
            continue
        
        # Calculate current value
        if market["status"] == "settled":
            # Settled: winning shares pay 1 token each
            if market["winner"] == "home":
                current_value = home_shares
            elif market["winner"] == "away":
                current_value = away_shares
            else:
                current_value = 0
        else:
            # Open/Closed: value at current market price (convert cents to tokens)
            current_value = ((home_shares * market["home_price"]) + (away_shares * market["away_price"])) / 100
        
        # Calculate potential return (best case) - 1 token per winning share
        potential_return = max(home_shares, away_shares)
        
        position = Position(
            market_id=market_id,
            game=f"{market['home_team']} vs {market['away_team']}",
            home_shares=home_shares,
            away_shares=away_shares,
            avg_home_price=pos.get("avg_home_price", 0),
            avg_away_price=pos.get("avg_away_price", 0),
            current_value=round(current_value, 2),
            potential_return=round(potential_return, 2)
        )
        
        if market["status"] == "settled":
            settled_positions.append(position)
        else:
            open_positions.append(position)
    
    total_value = balance + sum(p.current_value for p in open_positions)
    
    return Portfolio(
        user_id=user_id,
        balance=round(balance, 2),
        total_value=round(total_value, 2),
        open_positions=open_positions,
        settled_positions=settled_positions
    )


# ============== API ENDPOINTS ==============


@app.get("/")
async def root(user_id: Optional[str] = Cookie(None)):
    """Serve the frontend"""
    # Create user if doesn't exist
    user_id = get_or_create_user(user_id)
    
    if os.path.exists("static/index.html"):
        response = FileResponse("static/index.html")
        response.set_cookie(key="user_id", value=user_id, max_age=31536000)  # 1 year
        return response
    return {"message": "GT IM Prediction Market API", "docs": "/docs"}


@app.get("/api/user")
async def get_user(user_id: Optional[str] = Cookie(None)):
    """Get or create user"""
    user_id = get_or_create_user(user_id)
    user_data = users[user_id]
    
    response = JSONResponse({
        "user_id": user_id,
        "balance": round(user_data["balance"], 2),
        "created_at": user_data["created_at"]
    })
    response.set_cookie(key="user_id", value=user_id, max_age=31536000)
    return response


@app.get("/api/markets", response_model=MarketsResponse)
async def get_markets(user_id: Optional[str] = Cookie(None)):
    """Get all prediction markets"""
    # Ensure user exists
    user_id = get_or_create_user(user_id)
    
    # Update markets from cached games data (loaded on startup)
    if games_data:
        create_markets_from_games(games_data)
    
    market_list = [Market(**m) for m in markets.values()]
    
    open_count = sum(1 for m in market_list if m.status == 'open')
    closed_count = sum(1 for m in market_list if m.status == 'closed')
    settled_count = sum(1 for m in market_list if m.status == 'settled')
    
    return MarketsResponse(
        success=True,
        total_markets=len(market_list),
        open_markets=open_count,
        closed_markets=closed_count,
        settled_markets=settled_count,
        markets=market_list
    )


@app.post("/api/trade", response_model=TradeResponse)
async def execute_trade(trade: TradeRequest, user_id: Optional[str] = Cookie(None)):
    """Execute a prediction trade (buy shares)"""
    # Ensure user exists
    user_id = get_or_create_user(user_id)
    
    # Validate market exists
    if trade.market_id not in markets:
        raise HTTPException(status_code=404, detail="Market not found")
    
    market = markets[trade.market_id]
    
    # Check market is open
    if market["status"] != "open":
        raise HTTPException(status_code=400, detail=f"Market is {market['status']}, not accepting predictions")
    
    # Validate outcome
    if trade.outcome not in ['home', 'away']:
        raise HTTPException(status_code=400, detail="Outcome must be 'home' or 'away'")
    
    # Validate amount
    if trade.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    
    # Check user balance
    if users[user_id]["balance"] < trade.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    # Calculate shares to purchase
    current_home = market["home_shares"]
    current_away = market["away_shares"]
    
    if trade.outcome == 'home':
        current_outcome_shares = current_home
        other_shares = current_away
    else:
        current_outcome_shares = current_away
        other_shares = current_home
    
    # Binary search to find how many shares can be bought with the amount
    low, high = 0.0, trade.amount * 10  # Max possible shares
    shares_to_buy = 0.0
    
    for _ in range(100):  # Iterations for precision
        mid = (low + high) / 2
        cost = calculate_cost(current_outcome_shares, current_outcome_shares + mid, other_shares)
        
        if abs(cost - trade.amount) < 0.01:  # Close enough
            shares_to_buy = mid
            break
        elif cost < trade.amount:
            low = mid
            shares_to_buy = mid
        else:
            high = mid
    
    # Recalculate exact cost
    actual_cost = calculate_cost(current_outcome_shares, current_outcome_shares + shares_to_buy, other_shares)
    
    if shares_to_buy <= 0:
        raise HTTPException(status_code=400, detail="Amount too small to purchase shares")
    
    # Execute trade
    users[user_id]["balance"] -= actual_cost
    
    if trade.outcome == 'home':
        market["home_shares"] += shares_to_buy
    else:
        market["away_shares"] += shares_to_buy
    
    market["total_volume"] += actual_cost
    
    # Update prices
    home_price, away_price = calculate_lmsr_price(market["home_shares"], market["away_shares"])
    market["home_price"] = round(home_price, 2)
    market["away_price"] = round(away_price, 2)
    
    # Update user position
    if user_id not in user_positions:
        user_positions[user_id] = {}
    
    if trade.market_id not in user_positions[user_id]:
        user_positions[user_id][trade.market_id] = {
            "home_shares": 0,
            "away_shares": 0,
            "avg_home_price": 0,
            "avg_away_price": 0,
            "total_home_cost": 0,
            "total_away_cost": 0
        }
    
    position = user_positions[user_id][trade.market_id]
    
    if trade.outcome == 'home':
        old_shares = position["home_shares"]
        old_cost = position["total_home_cost"]
        position["home_shares"] += shares_to_buy
        position["total_home_cost"] += actual_cost
        position["avg_home_price"] = position["total_home_cost"] / position["home_shares"] if position["home_shares"] > 0 else 0
    else:
        old_shares = position["away_shares"]
        old_cost = position["total_away_cost"]
        position["away_shares"] += shares_to_buy
        position["total_away_cost"] += actual_cost
        position["avg_away_price"] = position["total_away_cost"] / position["away_shares"] if position["away_shares"] > 0 else 0
    
    # Calculate current value (convert cents to tokens)
    current_value = ((position["home_shares"] * market["home_price"]) + (position["away_shares"] * market["away_price"])) / 100
    # Calculate potential return - 1 token per winning share
    potential_return = max(position["home_shares"], position["away_shares"])
    
    new_position = Position(
        market_id=trade.market_id,
        game=f"{market['home_team']} vs {market['away_team']}",
        home_shares=position["home_shares"],
        away_shares=position["away_shares"],
        avg_home_price=round(position["avg_home_price"], 2),
        avg_away_price=round(position["avg_away_price"], 2),
        current_value=round(current_value, 2),
        potential_return=round(potential_return, 2)
    )
    
    price_per_share = actual_cost / shares_to_buy if shares_to_buy > 0 else 0
    
    return TradeResponse(
        success=True,
        shares_purchased=round(shares_to_buy, 2),
        price_per_share=round(price_per_share, 2),
        total_cost=round(actual_cost, 2),
        new_balance=round(users[user_id]["balance"], 2),
        new_position=new_position,
        message=f"Successfully purchased {round(shares_to_buy, 2)} shares"
    )


@app.get("/api/portfolio", response_model=Portfolio)
async def get_portfolio(user_id: Optional[str] = Cookie(None)):
    """Get user's portfolio"""
    user_id = get_or_create_user(user_id)
    return get_user_portfolio(user_id)


@app.get("/api/games", response_model=GamesResponse)
async def get_games():
    """
    Get games from memory (loaded from cache on startup)
    Use /api/games/refresh to fetch fresh data from API
    """
    
    try:
        # Return from in-memory games data
        if games_data:
            print(f"Returning {len(games_data)} games from memory")
            
            return GamesResponse(
                success=True,
                total_games=len(games_data),
                games=games_data,
                message=f"Loaded {len(games_data)} games from cache"
            )
        else:
            return GamesResponse(
                success=False,
                total_games=0,
                games=[],
                message="No cached data found. Use /api/games/refresh to fetch from API"
            )
    except Exception as e:
        print(f"Error reading cache: {e}")
        import traceback
        traceback.print_exc()
        return GamesResponse(
            success=False,
            total_games=0,
            games=[],
            message=f"Error reading cache: {str(e)}"
        )


@app.get("/api/games/refresh", response_model=GamesResponse)
async def refresh_games():
    """
    Fetch fresh games from IMLeagues API and save to cache
    
    This endpoint:
    1. Fetches games for each day in our range (last 3 days + next 7 days)
    2. Uses NewViewMode=0 to get only specific dates (more efficient than fetching full month)
    3. Parses the HTML to extract game data with dates and scores
    4. Saves to cache file for future requests
    5. Returns clean JSON with completed game scores
    """
    
    try:
        # Fetch games from API
        games = await fetch_all_games()
        
        # Save to cache file
        cache_data = {
            'games': [game.dict() for game in games],
            'count': len(games),
            'last_updated': str(datetime.now())
        }
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        # Update global games data and create/update markets
        global games_data
        games_data = games
        create_markets_from_games(games)
        
        print(f"Fetched and cached {len(games)} games, created/updated {len(games)} markets")
        
        return GamesResponse(
            success=True,
            total_games=len(games),
            games=games,
            message=f"Successfully fetched and cached {len(games)} games (last 3 days + next 7 days)"
        )
    except Exception as e:
        print(f"Error fetching games: {e}")
        import traceback
        traceback.print_exc()
        return GamesResponse(
            success=False,
            total_games=0,
            games=[],
            message=f"Error fetching games: {str(e)}"
        )


async def fetch_all_games() -> List[Game]:
    """
    Fetch games for each day in our date range using AjaxSearchGamesForSPAManageGames endpoint
    This is more efficient as it only fetches the exact dates we need (last 3 days + next 7 days)
    
    Returns:
        List of Game objects
    """
    from datetime import datetime, timedelta
    
    # Calculate date range: last 3 days to next 7 days
    today = datetime.now().date()
    start_date = today - timedelta(days=3)
    end_date = today + timedelta(days=7)
    
    all_games = []
    
    # Fetch games for each day in the range
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"\n=== Fetching games from {start_date} to {end_date} (day by day) ===")
        
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
            
            # Fetch games for this specific date
            games = await fetch_games_for_specific_date(client, date_str)
            
            if games:
                print(f"  {date_str}: {len(games)} games")
                all_games.extend(games)
            
            current_date += timedelta(days=1)
        
        print(f"Total games fetched: {len(all_games)}")
        return all_games


async def fetch_games_for_specific_date(client: httpx.AsyncClient, date_str: str) -> List[Game]:
    """
    Fetch games for a specific date
    
    Args:
        client: httpx AsyncClient to reuse connection
        date_str: Date string in format M/D/YYYY (e.g., "2/15/2026")
        
    Returns:
        List of Game objects for that date
    """
    url = "https://www.imleagues.com/AjaxPageRequestHandler.aspx"
    
    params = {
        "class": "imLeagues.Web.Members.Pages.BO.School.ManageGamesBO",
        "method": "AjaxSearchGamesForSPAManageGames"
    }
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.imleagues.com",
        "Referer": "https://www.imleagues.com/spa/intramural/13cc30785f6f4658aebbb07d83e19f67/managegames",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    
    # Using NewViewMode=0 returns only the selected date (more efficient!)
    payload = {
        "MemberId": "guest",
        "SchoolId": "13cc30785f6f4658aebbb07d83e19f67",
        "CategoryId": "0",
        "SportId": "",
        "LeagueId": "",
        "DivisionId": "",
        "FacilityId": "",
        "SurfaceId": "",
        "OfficialId": "",
        "CompleteGames": 0,
        "PublishedGames": 0,
        "StartDate": date_str,
        "EndDate": date_str,
        "ViewMode": "0",
        "SelectedDate": date_str,
        "ClubOrNot": "1",
        "RequestType": 1,
        "NewViewMode": 0  # Key: 0 = single date, 2 = full month
    }
    
    try:
        response = await client.post(url, params=params, json=payload, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        
        if not data.get('Data'):
            return []
        
        html_content = data['Data']
        
        # Parse HTML with BeautifulSoup
        games = parse_games_html_with_dates(html_content)
        
        return games
        
    except Exception as e:
        print(f"Error fetching games for {date_str}: {e}")
        return []


async def fetch_games_for_date(date_str: str) -> List[Game]:
    """
    Fetch games for a specific date
    
    Args:
        date_str: Date string in format MM/DD/YYYY
        
    Returns:
        List of Game objects
    """
    # IMLeagues API endpoint
    url = "https://www.imleagues.com/Services/AjaxRequestHandler.ashx"
    
    params = {
        "class": "imLeagues.Web.Members.Services.BO.Network.ManageGamesBO",
        "method": "Initialize",
        "paramType": "imLeagues.Internal.API.VO.Input.Network.ManageGamesViewInVO",
        "urlReferrer": "https://www.imleagues.com/spa/intramural/13cc30785f6f4658aebbb07d83e19f67/managegames"
    }
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.imleagues.com",
        "Referer": "https://www.imleagues.com/spa/intramural/13cc30785f6f4658aebbb07d83e19f67/managegames",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    
    payload = {
        "entityId": "13cc30785f6f4658aebbb07d83e19f67",
        "entityType": "intramural",
        "pageType": "Intramural",
        "resultsFilter": 0,
        "clientVersion": "572",
        "isMobileDevice": True,
        "isSSO": False,
        "cachedKey": None,
        "clientType": 0,
        "selectedDate": date_str  # Add the date parameter
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            print(f"\n=== Fetching games for date: {date_str} ===")
            print(f"Payload: {payload}")
            
            response = await client.post(url, params=params, json=payload, headers=headers)
            response.raise_for_status()
            
            # Parse JSON response
            data = response.json()
            
            # Extract HTML from the nested structure
            if "data" not in data or "manageGamesUCHtml" not in data["data"]:
                print(f"No games HTML found for {date_str}")
                return []
            
            html_content = data["data"]["manageGamesUCHtml"]
            print(f"HTML length for {date_str}: {len(html_content)} characters")
            
            # Parse HTML with BeautifulSoup
            games = parse_games_html(html_content, date_str)
            print(f"Parsed {len(games)} games for {date_str}")
            
            return games
            
    except Exception as e:
        print(f"Error fetching games for {date_str}: {e}")
        return []


def parse_games_html_with_dates(html_content: str) -> List[Game]:
    """
    Parse the HTML string to extract game information with proper date grouping
    
    Args:
        html_content: HTML string from the API response
        
    Returns:
        List of Game objects with proper dates from gameday attribute
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    games = []
    
    # Find all date sections (divs with gameday attribute)
    date_sections = soup.select('div[gameday]')
    
    print(f"Found {len(date_sections)} date sections")
    
    for date_section in date_sections:
        # Get the date for this section
        current_date = date_section.get('gameday')
        
        # Find all game containers within this date section
        # Use more flexible selector to catch all games
        game_elements = date_section.select('div.match')
        
        print(f"  Date {current_date}: {len(game_elements)} games")
        
        for game_elem in game_elements:
            try:
                # Extract game ID from data-id attribute
                game_id = game_elem.get('data-id', '')
                
                # Try multiple selectors for teams to handle different HTML structures
                # First try the specific structure with iml-team-left/right
                home_team_container = game_elem.select_one('div.iml-team-left')
                home_team_elem = home_team_container.select_one('a.teamHome') if home_team_container else None
                
                # Fallback to direct selector if specific structure not found
                if not home_team_elem:
                    home_team_elem = game_elem.select_one('a.teamHome, .teamHome')
                
                away_team_container = game_elem.select_one('div.iml-team-right')
                away_team_elem = away_team_container.select_one('a.teamAway') if away_team_container else None
                
                # Fallback to direct selector if specific structure not found
                if not away_team_elem:
                    away_team_elem = game_elem.select_one('a.teamAway, .teamAway')
                
                if not home_team_elem or not away_team_elem:
                    continue
                
                home_team = home_team_elem.get_text(strip=True)
                away_team = away_team_elem.get_text(strip=True)
                
                # Extract scores - CRITICAL: Use .get_text() to recursively extract from nested spans
                # The score might be directly in <strong> OR nested in <span class='match-win'>
                home_score_elem = game_elem.select_one('strong.match-team1Score, .match-team1Score')
                away_score_elem = game_elem.select_one('strong.match-team2Score, .match-team2Score')
                
                # Use .get_text(strip=True) to recursively extract text from nested elements
                home_score_text = home_score_elem.get_text(strip=True) if home_score_elem else "--"
                away_score_text = away_score_elem.get_text(strip=True) if away_score_elem else "--"
                
                # Check for forfeit/default indicators
                forfeit_elem = game_elem.select_one('small.text-muted')
                forfeit_text = forfeit_elem.get_text(strip=True).lower() if forfeit_elem else ""
                is_forfeit = 'forfeit' in forfeit_text or 'default' in forfeit_text
                
                # Determine status based on score values and forfeit status
                if home_score_text == "--" and away_score_text == "--":
                    if is_forfeit:
                        status = "forfeit"
                        home_score = "--"
                        away_score = "--"
                    else:
                        status = "scheduled"
                        home_score = "--"
                        away_score = "--"
                elif home_score_text.isdigit() and away_score_text.isdigit():
                    if is_forfeit:
                        status = "forfeit"
                    else:
                        status = "completed"
                    home_score = home_score_text
                    away_score = away_score_text
                else:
                    # Handle partial scores or other edge cases
                    if is_forfeit:
                        status = "forfeit"
                    else:
                        status = "unknown"
                    home_score = home_score_text
                    away_score = away_score_text
                
                # Extract time
                time_elem = game_elem.select_one('.time')
                game_time = time_elem.get_text(strip=True) if time_elem else "TBD"
                
                # Extract sport (from the sport link)
                sport_elem = game_elem.select_one('a[href*="/sport/"]')
                sport = sport_elem.get_text(strip=True) if sport_elem else "Unknown"
                
                # Extract location/venue (facility + court)
                facility_elem = game_elem.select_one('.match-facility')
                court_elem = game_elem.select_one('.iml-game-court')
                
                if facility_elem and court_elem:
                    facility = facility_elem.get_text(strip=True)
                    court = court_elem.get_text(strip=True)
                    location = f"{facility}, {court}"
                elif facility_elem:
                    location = facility_elem.get_text(strip=True)
                else:
                    location = None
                
                # Extract league info
                league_elem = game_elem.select_one('a[href*="/league/"]')
                league = league_elem.get_text(strip=True) if league_elem else None
                
                # Extract team records (W-L-T format)
                # Records are in <small class="text-muted"> within each team's .media container
                home_record = None
                away_record = None
                
                # Find all .media containers within the game (one for home, one for away)
                team_media_containers = game_elem.find_all('div', class_='media')
                
                # The first .media should be home team, second should be away team
                for media in team_media_containers:
                    # Check if this media contains the home team or away team
                    team_link = media.select_one('.teamHome, .teamAway')
                    if not team_link:
                        continue
                    
                    # Find the record in this media's body
                    media_body = media.select_one('.media-body')
                    if media_body:
                        record_elem = media_body.select_one('small.text-muted')
                        if record_elem:
                            record_text = record_elem.get_text(strip=True)
                            # Only capture if it looks like a record (contains digits and hyphens)
                            if '-' in record_text and '(' in record_text:
                                # Determine if this is home or away based on the team class
                                if 'teamHome' in team_link.get('class', []):
                                    home_record = record_text
                                elif 'teamAway' in team_link.get('class', []):
                                    away_record = record_text
                
                game = Game(
                    game_id=game_id,
                    home_team=home_team,
                    away_team=away_team,
                    home_score=home_score,
                    away_score=away_score,
                    time=game_time,
                    date=current_date,
                    sport=sport,
                    status=status,
                    location=location,
                    league=league,
                    home_record=home_record,
                    away_record=away_record
                )
                
                games.append(game)
                
            except Exception as e:
                # Skip games that fail to parse
                print(f"Error parsing game: {e}")
                continue
    
    return games


def parse_games_html(html_content: str, date_str: str = None) -> List[Game]:
    """
    Parse the HTML string to extract game information
    
    Args:
        html_content: HTML string from the API response
        date_str: Date string to use for all games (passed from fetch_games_for_date)
        
    Returns:
        List of Game objects
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    games = []
    
    # Use the date_str parameter that was passed in, which corresponds to the date we requested
    # Don't extract from HTML as it may not reflect the selectedDate parameter
    current_date = date_str
    
    # Only fall back to HTML if date_str wasn't provided
    if not current_date:
        date_elem = soup.select_one('#pNowDate')
        current_date = date_elem.get_text(strip=True) if date_elem else None
    
    if not current_date:
        game_day_elem = soup.select_one('[gameday]')
        if game_day_elem:
            current_date = game_day_elem.get('gameday')
    
    # Find all game containers (divs with class 'match')
    game_elements = soup.select('div.match')
    
    for game_elem in game_elements:
        try:
            # Extract game ID
            game_id = game_elem.get('data-id', '')
            
            # Extract teams
            home_team_elem = game_elem.select_one('.teamHome')
            away_team_elem = game_elem.select_one('.teamAway')
            
            if not home_team_elem or not away_team_elem:
                continue
            
            home_team = home_team_elem.get_text(strip=True)
            away_team = away_team_elem.get_text(strip=True)
            
            # Extract scores
            home_score_elem = game_elem.select_one('.match-team1Score')
            away_score_elem = game_elem.select_one('.match-team2Score')
            
            home_score = home_score_elem.get_text(strip=True) if home_score_elem else "--"
            away_score = away_score_elem.get_text(strip=True) if away_score_elem else "--"
            
            # Extract time
            time_elem = game_elem.select_one('.time')
            game_time = time_elem.get_text(strip=True) if time_elem else "TBD"
            
            # Extract sport (from the sport link)
            sport_elem = game_elem.select_one('a[href*="/sport/"]')
            sport = sport_elem.get_text(strip=True) if sport_elem else "Unknown"
            
            # Extract location/venue
            location_elem = game_elem.select_one('.location, .venue')
            location = location_elem.get_text(strip=True) if location_elem else None
            
            # Extract league info
            league_elem = game_elem.select_one('a[href*="/league/"]')
            league = league_elem.get_text(strip=True) if league_elem else None
            
            # Determine status
            if home_score == "--" or away_score == "--":
                status = "scheduled"
            elif home_score.isdigit() and away_score.isdigit():
                # Check if game is complete or in progress
                # For now, assume any game with scores is complete
                # You could add more logic here based on additional fields
                status = "completed"
            else:
                status = "unknown"
            
            game = Game(
                game_id=game_id,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
                time=game_time,
                date=current_date,
                sport=sport,
                status=status,
                location=location,
                league=league
            )
            
            games.append(game)
            
        except Exception as e:
            # Skip games that fail to parse
            print(f"Error parsing game: {e}")
            continue
    
    return games


@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "GT IM Prediction Market API"}


@app.on_event("startup")
async def startup_event():
    """Load games from cache on startup"""
    global games_data
    
    if CACHE_FILE.exists():
        print(f"Loading games from cache: {CACHE_FILE}")
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
            games_data = [Game(**game) for game in data.get('games', [])]
            create_markets_from_games(games_data)
            print(f"Loaded {len(games_data)} games and created {len(markets)} markets")
    else:
        print("No cache file found. Use /api/games/refresh to fetch games.")
        games_data = []


if __name__ == "__main__":
    import uvicorn
    # Set reload=False for stability during testing
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
