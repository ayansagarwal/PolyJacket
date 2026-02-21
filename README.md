# ⚡ PolyJacket - GT Intramural Prediction Market

A full-featured prediction market platform for Georgia Tech Intramural sports, built with FastAPI and Vue.js. Trade shares on game outcomes using our in-game currency "tokens" (🪙).

## ✅ Project Status

**FULLY OPERATIONAL** - Complete prediction market platform:
- ✅ Automated Market Maker (LMSR) for dynamic pricing
- ✅ User wallets with 10,000 tokens starting balance
- ✅ Real-time market updates from IMLeagues API
- ✅ Portfolio tracking with open and settled positions
- ✅ Markets auto-close at game start time
- ✅ Automatic settlement based on final scores
- ✅ 166 active markets (45 open, 40 closed, 81 settled)

## Features

### 🎯 Prediction Markets
- **Binary Markets**: Predict which team will win each game
- **Dynamic Pricing**: Logarithmic Market Scoring Rule (LMSR) adjusts prices based on trading activity
- **Market States**:
  - 🟢 **Open**: Markets accepting predictions until game start
  - 🔴 **Closed**: Game started, awaiting final score
  - ✅ **Settled**: Final score recorded, payouts distributed

### 💰 Trading System
- **Virtual Currency**: "tokens" (🪙) - GT-themed in-game currency
- **Starting Balance**: 10,000 tokens per user
- **Share Pricing**: 0-100¢ per share based on market probability
- **Settlement**: Winning shares pay 100 tokens, losing shares pay 0

### 📊 Portfolio Management
- Track open positions across multiple markets
- View settled positions and payouts
- Real-time portfolio valuation
- Detailed performance analytics

## Tech Stack

- **Backend**: FastAPI (Python 3.13), Pydantic
- **Frontend**: Vue.js 3 (Composition API)
- **Market Maker**: LMSR (Logarithmic Market Scoring Rule)
- **Data Source**: IMLeagues API with BeautifulSoup4 parsing
- **HTTP Client**: httpx for async API calls
- **Server**: Uvicorn ASGI

## How It Works

### Market Maker (LMSR)
We use the Logarithmic Market Scoring Rule to provide liquidity:
- Prices automatically adjust based on share purchases
- More shares bought = higher price for that outcome
- Ensures prices always sum to 100¢
- Parameter `b=100` controls liquidity depth

### Market Lifecycle
1. **Created**: When game data is fetched from IMLeagues
2. **Open**: Users can buy shares on either team
3. **Closed**: Game starts (based on scheduled time)
4. **Settled**: Final score recorded, winning shares pay 100 tokens

### User Experience
- Each user gets a unique ID stored in cookies
- Starting balance: 10,000 tokens
- Buy shares in any open market
- Watch prices update in real-time
- Track positions in your portfolio
- Collect payouts when markets settle

## Setup & Installation

1. **Create/Activate Virtual Environment** (already configured):
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

2. **Install Dependencies** (already installed):
   ```powershell
   pip install -r requirements.txt
   ```

3. **Run the Server**:
   ```powershell
   python main.py
   ```
   
   Or using the venv directly:
   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

## Usage

1. **Start the server** (runs on http://localhost:8000):
   ```powershell
   python main.py
   ```

2. **Access the application**:
   - **Main App**: http://localhost:8000/
   - **API Docs**: http://localhost:8000/docs
   - **Health Check**: http://localhost:8000/api/health

3. **Start Predicting**:
   - Browse open markets on the Markets tab
   - Click "Buy" on your predicted winner
   - Enter amount in tokens to spend
   - Confirm prediction
   - Track positions in Portfolio tab

4. **API Endpoints**:
   - `GET /api/user` - Get/create user and balance
   - `GET /api/markets` - Get all prediction markets
   - `POST /api/trade` - Execute a prediction (buy shares)
   - `GET /api/portfolio` - Get user's positions
   - `GET /api/games` - Get cached games data
   - `GET /api/games/refresh` - Fetch fresh data from IMLeagues

5. **Trading Example**:
   ```powershell
   # Place a prediction via API
   Invoke-RestMethod -Uri http://localhost:8000/api/trade -Method POST -Headers @{"Content-Type"="application/json"} -Body '{"market_id":"market_R23324077","outcome":"home","amount":500}'
   ```

## File Structure

```
PolyJacket/
├── main.py              # FastAPI backend with prediction market logic
│                        # - User management & wallets
│                        # - LMSR market maker
│                        # - Trade execution
│                        # - Market lifecycle management
├── requirements.txt     # Python dependencies
├── games_cache.json     # Cached games data from IMLeagues
├── static/
│   └── index.html       # Vue.js 3 SPA with prediction interface
├── .venv/               # Virtual environment
└── .gitignore           # Git ignore rules

```

## Market Data

- **Total Markets**: 166 games
- **Date Range**: Last 3 days + Next 7 days from current date
- **Sports**: Basketball, Flag Football, Soccer, Volleyball, etc.
- **Data Source**: IMLeagues API for Georgia Tech
- **Update Frequency**: On-demand via `/api/games/refresh`

## Development

### Refresh Games & Markets
To fetch the latest games and create/update markets:
```bash
curl http://localhost:8000/api/games/refresh
```

### Test Trading
```powershell
# Get your user ID and balance
$user = Invoke-RestMethod -Uri http://localhost:8000/api/user
Write-Host "Balance: $($user.balance) tokens"

# View available markets
$markets = Invoke-RestMethod -Uri http://localhost:8000/api/markets
$markets.markets | Where-Object {$_.status -eq 'open'} | Select-Object -First 5

# Place a prediction
$trade = @{
  market_id = "market_R23324077"
  outcome = "home"
  amount = 500
} | ConvertTo-Json

Invoke-RestMethod -Uri http://localhost:8000/api/trade -Method POST `
  -Headers @{"Content-Type"="application/json"} -Body $trade

# Check portfolio
Invoke-RestMethod -Uri http://localhost:8000/api/portfolio
```

### Understanding Market Prices
- Prices range from 0-100¢ (representing probability)
- 50¢ = 50% implied probability
- Buying shares increases price
- YES and NO prices always sum to 100¢
- Higher volume = more accurate prices

## Architecture Notes

### In-Memory Storage
- User data, markets, and positions stored in dictionaries
- **Production**: Replace with PostgreSQL/MongoDB
- **Persistence**: Add database layer for user balances and positions
- **Scalability**: Current implementation supports demo/testing

### Market Maker Details
- **Algorithm**: LMSR (Logarithmic Market Scoring Rule)
- **Liquidity Parameter**: b = 100 (adjustable)
- **Price Calculation**: `price = exp(shares/b) / (exp(yes/b) + exp(no/b))`
- **Cost Function**: Binary search to determine shares for given amount

### Settlement Logic
- Triggers when game score is finalized
- Winning shares worth 100 tokens
- Losing shares worth 0 tokens
- Automatic payout to user balance (future feature)

## Notes

- The project uses `.venv` for the Python virtual environment
- Port 8000 is the default - ensure it's not in use by other applications
- Markets automatically close at scheduled game start time
- All "betting" terminology has been removed per prediction market standards
- Currency: "tokens" (🪙) - GT-themed virtual currency
- Frontend built with Vue 3 Composition API for reactive state management

## Verified Working ✓

All systems tested and operational:
- [x] User creation with 10,000 token starting balance
- [x] Market creation from IMLeagues game data
- [x] LMSR pricing algorithm functioning correctly
- [x] Trade execution and share calculations
- [x] Portfolio tracking (open & settled positions)
- [x] Market lifecycle (open → closed → settled)
- [x] Automatic settlement based on final scores
- [x] Frontend prediction interface
- [x] Real-time price updates
- [x] Mobile-responsive design

## Future Enhancements

- [ ] Database persistence (PostgreSQL)
- [ ] User authentication & accounts
- [ ] Leaderboard system
- [ ] Historical performance tracking
- [ ] Mobile app (React Native)
- [ ] Push notifications for market settlements
- [ ] Live game updates integration
- [ ] Advanced charting for price history
- [ ] Social features (following users, sharing predictions)
- [ ] Tournament mode with prizes
