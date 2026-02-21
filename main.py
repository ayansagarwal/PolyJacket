"""
FastAPI Backend for Georgia Tech IM Prediction Market
Fetches game data from IMLeagues API endpoint
.
"""

from fastapi import FastAPI, HTTPException, Cookie, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import httpx
from bs4 import BeautifulSoup
from typing import List, Optional, Dict
from pydantic import BaseModel, EmailStr
import os
import json
from pathlib import Path
from datetime import datetime, timedelta
import uuid
import math
import csv
import asyncio

# Import authentication and database modules
import database as db
import auth

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
CACHE_FILE = Path("data/games_cache.json")
ELO_RATINGS_FILE = Path("data/elo_ratings.csv")

# In-memory storage (use database in production)
users: Dict[str, dict] = {}
markets: Dict[str, dict] = {}
user_positions: Dict[str, dict] = {}  # user_id -> {market_id: {home_shares, away_shares}}
games_data = []  # Cached games loaded on startup (List[Game])
elo_data: Dict[str, Dict[str, float]] = {}  # sport -> team -> elo rating

# Constants
STARTING_BALANCE = 10000  # Starting tokens for new users
LIQUIDITY_PARAMETER = 100  # For AMM pricing (b parameter in LMSR)
ELO_BASE = 1000  # Default Elo rating for unknown teams
REFRESH_INTERVAL_MINUTES = 5  # How often to auto-refresh games from IMLeagues

# Team names that represent placeholder/unscheduled slots — never create markets for these
GENERIC_TEAMS = {"tbd", "bye", "generic team", "unknown", "home", "away", "team", ""}


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
    home_elo: Optional[float] = None  # Elo rating for home team
    away_elo: Optional[float] = None  # Elo rating for away team


class Position(BaseModel):
    """User's position in a market"""
    market_id: str
    game: str
    home_shares: float
    away_shares: float
    avg_home_price: float
    avg_away_price: float
    potential_payout: float


class Portfolio(BaseModel):
    """User's portfolio"""
    user_id: str
    balance: float
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
    home_elo: Optional[float] = None
    away_elo: Optional[float] = None


class MarketsResponse(BaseModel):
    """Response with all markets"""
    success: bool
    total_markets: int
    open_markets: int
    closed_markets: int
    settled_markets: int
    markets: List[Market]


class RegisterRequest(BaseModel):
    """Request to register a new user"""
    username: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    """Request to login"""
    username: str
    password: str


# ============== AUTHENTICATION ==============

async def get_current_user(authorization: Optional[str] = Header(None)) -> Optional[Dict]:
    """Dependency to get current authenticated user from JWT token"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    token = authorization.replace("Bearer ", "")
    token_data = auth.decode_access_token(token)
    
    if token_data is None:
        return None
    
    user = db.get_user_by_id(token_data.user_id)
    return user


# ============== UTILITY FUNCTIONS ==============

def elo_win_prob(rating_a: float, rating_b: float) -> float:
    """Expected probability that team A beats team B (standard Elo formula)."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def load_elo_data():
    """Load Elo ratings from CSV into the global elo_data dict."""
    global elo_data
    elo_data = {}
    try:
        with open(ELO_RATINGS_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sport = row['sport']
                team = row['team']
                elo = float(row['elo'])
                if sport not in elo_data:
                    elo_data[sport] = {}
                elo_data[sport][team] = elo
        total_teams = sum(len(v) for v in elo_data.values())
        print(f"Loaded Elo ratings for {total_teams} teams across {len(elo_data)} sports")
    except FileNotFoundError:
        print(f"Elo ratings file not found: {ELO_RATINGS_FILE} — using default rating {ELO_BASE} for all teams")


def get_elo_seeded_shares(home_team: str, away_team: str, sport: str, b: float = LIQUIDITY_PARAMETER):
    """
    Compute initial LMSR share quantities so the opening price equals the
    Elo-derived win probability.

    Derivation: LMSR price = exp(q_home/b) / (exp(q_home/b) + exp(q_away/b))
    Setting this equal to p_home and fixing q_away = 0 (or q_home = 0 for the
    underdog) gives: q_favored = b * ln(p_favored / p_underdog).

    With b=100 this seeding costs the house ≈ b * ln(1/(2*p_underdog)) tokens —
    roughly 0–100 tokens for matchups up to 80/20, which matches the
    LIQUIDITY_PARAMETER naturally.

    Returns:
        (home_shares, away_shares, home_elo, away_elo)
    """
    home_elo_val = elo_data.get(sport, {}).get(home_team, ELO_BASE)
    away_elo_val = elo_data.get(sport, {}).get(away_team, ELO_BASE)

    p_home = elo_win_prob(home_elo_val, away_elo_val)
    p_away = 1.0 - p_home

    if p_home >= p_away:
        home_shares = b * math.log(p_home / p_away)
        away_shares = 0.0
    else:
        home_shares = 0.0
        away_shares = b * math.log(p_away / p_home)

    # LMSR cost of moving from (0,0) to the seeded position — this is the
    # market-maker's effective investment and should count as initial volume.
    seeded_q = home_shares if home_shares > 0 else away_shares
    if seeded_q > 0:
        initial_volume = b * math.log(math.exp(seeded_q / b) + 1.0) - b * math.log(2.0)
    else:
        initial_volume = 0.0

    return home_shares, away_shares, round(home_elo_val, 1), round(away_elo_val, 1), round(initial_volume, 4)


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


def calculate_sell_value(user_shares: float, current_side_shares: float, other_shares: float, b: float = LIQUIDITY_PARAMETER) -> float:
    """
    Calculate tokens received for selling user_shares at the current market state.
    Uses the LMSR sell-back formula: value = C(q−Δq → q).
    This correctly accounts for market impact and will always be ≤ the original
    purchase cost (no reverse-arbitrage), rather than the inflated
    shares × marginal_price formula which overestimates value.
    """
    try:
        before = b * math.log(math.exp(current_side_shares / b) + math.exp(other_shares / b))
        after  = b * math.log(math.exp((current_side_shares - user_shares) / b) + math.exp(other_shares / b))
        return max(0.0, before - after)
    except:
        return 0.0


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
        elif game_time in ["FINAL", "BYE", "FORFEIT"]:
            return True
        return False
    except:
        return False


def create_markets_from_games(games: List[Game]):
    """Create or update markets from game data"""
    for game in games:
        # Skip placeholder/BYE/TBD matchups
        if (game.home_team.strip().lower() in GENERIC_TEAMS or
                game.away_team.strip().lower() in GENERIC_TEAMS):
            continue

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
        
        # Check if market exists
        existing_market = db.get_market(market_id)
        
        if not existing_market:
            # Seed initial shares from Elo so opening price == Elo win probability
            init_home_shares, init_away_shares, home_elo_val, away_elo_val, seed_volume = get_elo_seeded_shares(
                game.home_team, game.away_team, game.sport
            )
            
            # Calculate prices
            home_price, away_price = calculate_lmsr_price(init_home_shares, init_away_shares)
            
            market_data = {
                "market_id": market_id,
                "game_id": game.game_id,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "sport": game.sport,
                "game_time": game.time,
                "game_date": game.date or "",
                "status": status,
                "home_shares": init_home_shares,
                "away_shares": init_away_shares,
                "total_volume": seed_volume,
                "winner": winner,
                "home_score": game.home_score,
                "away_score": game.away_score,
                "home_elo": home_elo_val,
                "away_elo": away_elo_val,
                "home_price": round(home_price, 2),
                "away_price": round(away_price, 2)
            }
        else:
            # Update status, winner, and scores (keep existing shares / elo)
            market_data = dict(existing_market)
            market_data["status"] = status
            market_data["winner"] = winner
            market_data["home_score"] = game.home_score
            market_data["away_score"] = game.away_score
            
            # Recalculate prices
            home_price, away_price = calculate_lmsr_price(
                market_data["home_shares"],
                market_data["away_shares"]
            )
            market_data["home_price"] = round(home_price, 2)
            market_data["away_price"] = round(away_price, 2)
        
        # Save to database
        db.upsert_market(market_data)


def get_user_portfolio(user_id: int) -> Portfolio:
    """Get user's complete portfolio"""
    user = db.get_user_by_id(user_id)
    if not user:
        return Portfolio(
            user_id=str(user_id),
            balance=0,
            open_positions=[],
            settled_positions=[]
        )
    
    balance = user["balance"]
    open_positions = []
    settled_positions = []
    
    # Get user positions with joined market data
    positions = db.get_user_positions(user_id)
    
    for pos_market in positions:
        home_shares = pos_market.get("home_shares", 0)
        away_shares = pos_market.get("away_shares", 0)
        
        if home_shares == 0 and away_shares == 0:
            continue
        
        market_status = pos_market["status"]
        
        # Calculate potential payout - 1 token per winning share
        # This is what user gets if their prediction is correct
        if market_status == "settled":
            # Already settled - show actual payout
            if pos_market["winner"] == "home":
                potential_payout = home_shares
            elif pos_market["winner"] == "away":
                potential_payout = away_shares
            else:
                potential_payout = 0
        else:
            # Open/Closed: show max possible payout
            potential_payout = max(home_shares, away_shares)
        
        position = Position(
            market_id=pos_market["market_id"],
            game=f"{pos_market['home_team']} vs {pos_market['away_team']}",
            home_shares=home_shares,
            away_shares=away_shares,
            avg_home_price=pos_market.get("avg_home_price", 0),
            avg_away_price=pos_market.get("avg_away_price", 0),
            potential_payout=round(potential_payout, 2)
        )
        
        if market_status == "settled":
            settled_positions.append(position)
        else:
            open_positions.append(position)
    
    return Portfolio(
        user_id=str(user_id),
        balance=round(balance, 2),
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


@app.post("/api/register", response_model=auth.Token)
async def register(request: RegisterRequest):
    """Register a new user"""
    # Check if username or email already exists
    if db.get_user_by_username(request.username):
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # Hash password and create user
    hashed_password = auth.get_password_hash(request.password)
    user_id = db.create_user(request.username, request.email, hashed_password, STARTING_BALANCE)
    
    if user_id is None:
        raise HTTPException(status_code=400, detail="Registration failed")
    
    # Create access token
    access_token = auth.create_access_token(
        data={"sub": str(user_id), "username": request.username}
    )
    
    return auth.Token(
        access_token=access_token,
        token_type="bearer",
        user_id=user_id,
        username=request.username
    )


@app.post("/api/login", response_model=auth.Token)
async def login(request: LoginRequest):
    """Login with username and password"""
    # Get user
    user = db.get_user_by_username(request.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    # Verify password
    if not auth.verify_password(request.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    # Update last login
    db.update_last_login(user["id"])
    
    # Create access token
    access_token = auth.create_access_token(
        data={"sub": str(user["id"]), "username": user["username"]}
    )
    
    return auth.Token(
        access_token=access_token,
        token_type="bearer",
        user_id=user["id"],
        username=user["username"]
    )


@app.get("/api/user")
async def get_user(user: Optional[Dict] = Depends(get_current_user)):
    """Get current authenticated user"""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    return {
        "user_id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "balance": round(user["balance"], 2),
        "created_at": user["created_at"]
    }


@app.get("/api/markets", response_model=MarketsResponse)
async def get_markets():
    """Get all prediction markets"""
    # Update markets from cached games data (loaded on startup)
    if games_data:
        create_markets_from_games(games_data)
    
    # Get markets from database
    all_markets = db.get_all_markets()
    market_list = [Market(**m) for m in all_markets]
    
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
async def execute_trade(trade: TradeRequest, user: Optional[Dict] = Depends(get_current_user)):
    """Execute a prediction trade (buy shares)"""
    # Require authentication
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    user_id = user["id"]
    
    # Validate market exists
    market = db.get_market(trade.market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
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
    if user["balance"] < trade.amount:
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
    new_balance = user["balance"] - actual_cost
    db.update_user_balance(user_id, new_balance)
    
    if trade.outcome == 'home':
        market["home_shares"] += shares_to_buy
    else:
        market["away_shares"] += shares_to_buy
    
    market["total_volume"] += actual_cost
    
    # Update prices
    home_price, away_price = calculate_lmsr_price(market["home_shares"], market["away_shares"])
    market["home_price"] = round(home_price, 2)
    market["away_price"] = round(away_price, 2)
    
    # Save updated market to database
    db.upsert_market(market)
    
    # Update user position
    position = db.get_position(user_id, trade.market_id)
    
    if not position:
        position = {
            "home_shares": 0,
            "away_shares": 0,
            "avg_home_price": 0,
            "avg_away_price": 0,
            "total_home_cost": 0,
            "total_away_cost": 0
        }
    else:
        position["total_home_cost"] = position.get("avg_home_price", 0) * position.get("home_shares", 0)
        position["total_away_cost"] = position.get("avg_away_price", 0) * position.get("away_shares", 0)
    
    if trade.outcome == 'home':
        position["home_shares"] += shares_to_buy
        position["total_home_cost"] += actual_cost
        position["avg_home_price"] = position["total_home_cost"] / position["home_shares"] if position["home_shares"] > 0 else 0
    else:
        position["away_shares"] += shares_to_buy
        position["total_away_cost"] += actual_cost
        position["avg_away_price"] = position["total_away_cost"] / position["away_shares"] if position["away_shares"] > 0 else 0
    
    # Save position to database
    db.upsert_position(
        user_id=user_id,
        market_id=trade.market_id,
        home_shares=position["home_shares"],
        away_shares=position["away_shares"],
        avg_home_price=position["avg_home_price"],
        avg_away_price=position["avg_away_price"]
    )
    
    # Calculate current value using LMSR sell-back (always <= what was paid)
    home_val = calculate_sell_value(position["home_shares"], market["home_shares"], market["away_shares"]) if position["home_shares"] > 0 else 0.0
    away_val = calculate_sell_value(position["away_shares"], market["away_shares"], market["home_shares"]) if position["away_shares"] > 0 else 0.0
    current_value = home_val + away_val
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
        new_balance=round(new_balance, 2),
        new_position=new_position,
        message=f"Successfully purchased {round(shares_to_buy, 2)} shares",
        home_elo=market.get("home_elo"),
        away_elo=market.get("away_elo"),
    )


@app.get("/api/portfolio", response_model=Portfolio)
async def get_portfolio(user: Optional[Dict] = Depends(get_current_user)):
    """Get user's portfolio"""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    return get_user_portfolio(user["id"])


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
                
                # Extract time — IMLeagues uses span.status for scheduled time
                # (it shows the kickoff time for future games, e.g. "7:00 PM",
                #  and "FINAL" for completed ones — we keep whatever string is there)
                time_elem = game_elem.select_one('span.status, .iml-game-time, .match-time, .time')
                game_time = time_elem.get_text(strip=True) if time_elem else "TBD"
                # Normalise: blank or placeholder strings → TBD
                if not game_time or game_time in ("-", "--"):
                    game_time = "TBD"
                
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
    """Initialize database, load Elo ratings, seed from cache, then start background refresh loop"""
    global games_data

    # Initialize database
    db.init_database()

    # Load Elo ratings first so market seeding has data
    load_elo_data()

    # Seed from cache immediately so the server is ready before the first live fetch
    if CACHE_FILE.exists():
        print(f"Seeding from cache: {CACHE_FILE}")
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
            games_data = [Game(**game) for game in data.get('games', [])]
            create_markets_from_games(games_data)
            print(f"Seeded {len(games_data)} games and {len(db.get_all_markets())} markets from cache")
    else:
        print("No cache file found. Will fetch from API...")

    # Kick off the background refresh loop (first run is immediate)
    asyncio.create_task(_refresh_loop())


async def _refresh_loop():
    """Background task: fetch fresh games from IMLeagues, then repeat every REFRESH_INTERVAL_MINUTES."""
    global games_data
    while True:
        try:
            print(f"[refresh] Fetching live games from IMLeagues...")
            fresh_games = await fetch_all_games()
            if fresh_games:
                games_data = fresh_games
                cache_data = {
                    'games': [g.dict() for g in fresh_games],
                    'count': len(fresh_games),
                    'last_updated': str(datetime.now())
                }
                with open(CACHE_FILE, 'w') as f:
                    json.dump(cache_data, f, indent=2)
                create_markets_from_games(games_data)
                print(f"[refresh] Updated {len(fresh_games)} games and {len(markets)} markets")
            else:
                print("[refresh] No games returned; keeping existing data")
        except Exception as e:
            print(f"[refresh] Error during background refresh: {e}")
        await asyncio.sleep(REFRESH_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import uvicorn
    # Set reload=False for stability during testing
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
