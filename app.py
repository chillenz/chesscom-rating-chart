from flask import Flask, render_template, request, jsonify
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry
import pandas as pd
import datetime
import plotly.graph_objects as go
import plotly.io as pio
from concurrent.futures import ThreadPoolExecutor
import logging
from functools import lru_cache
import re
import time

app = Flask(__name__)

# Configuration
CHESS_API_BASE = "https://api.chess.com/pub/player"
MAX_WORKERS = 10

# Simple in-memory cache
class SimpleCache:
    def __init__(self, timeout=300):
        self.cache = {}
        self.timeout = timeout
    
    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.timeout:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = (value, time.time())

# Set up simple caching
cache = SimpleCache(timeout=300)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up session with connection pooling and retries
session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://", adapter)

class ChessAPIError(Exception):
    pass

def validate_username(username):
    """Validate chess.com username format"""
    if not username or len(username) < 2 or len(username) > 30:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', username))

@lru_cache(maxsize=128)
def get_user_profile(username):
    """Get user profile with caching"""
    url = f"{CHESS_API_BASE}/{username}"
    headers = {'User-Agent': 'ChessTracker/1.0'}
    
    try:
        response = session.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise ChessAPIError("User not found")
        else:
            raise ChessAPIError(f"API error: {response.status_code}")
    except requests.exceptions.RequestException as e:
        raise ChessAPIError(f"Network error: {str(e)}")

def get_user_archives(username):
    """Get list of months with available games"""
    url = f"{CHESS_API_BASE}/{username}/games/archives"
    headers = {'User-Agent': 'ChessTracker/1.0'}
    
    try:
        response = session.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            archives = response.json().get('archives', [])
            # Parse URLs like ".../games/2024/01"
            months = []
            for archive in archives:
                parts = archive.split('/')
                year, month = int(parts[-2]), int(parts[-1])
                months.append((year, month))
            return months
        else:
            logger.warning(f"Failed to get archives: {response.status_code}")
            return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed for archives: {str(e)}")
        return []

def get_single_month_games(args):
    """Fetch games for a single month and filter by time class"""
    username, year, month, time_class = args
    try:
        url = f"{CHESS_API_BASE}/{username}/games/{year}/{month:02d}"
        headers = {'User-Agent': 'ChessTracker/1.0'}
        
        response = session.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            games = response.json().get("games", [])
            # Filter immediately by time class
            return [g for g in games if g.get("time_class") == time_class]
        elif response.status_code != 404:
            logger.warning(f"API error for {username}/{year}/{month}: {response.status_code}")
        return []
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed for {username}/{year}/{month}: {str(e)}")
        return []

def extract_game_data(games, username):
    """Extract game data efficiently"""
    if not games:
        return []
    
    data = []
    username_lower = username.lower()
    
    for game in games:
        white_username = game.get("white", {}).get("username", "").lower()
        if white_username == username_lower:
            player_data = game["white"]
        else:
            player_data = game["black"]
        
        end_time = game.get("end_time")
        if end_time:
            ts = datetime.datetime.fromtimestamp(end_time)
            data.append({
                "date": ts.date(),
                "rating": player_data.get("rating", 0),
                "timestamp": ts,
                "year": ts.year,
                "month": ts.month
            })
    
    return data

def create_candlestick_chart(df, time_control):
    """Create proper OHLC candlestick chart with vectorized operations"""
    if df.empty:
        return None
    
    # Convert to datetime and sort
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('timestamp')
    
    # Vectorized OHLC aggregation
    ohlc_df = df.groupby('date')['rating'].agg([
        ('open', 'first'),
        ('high', 'max'),
        ('low', 'min'),
        ('close', 'last')
    ]).reset_index()
    
    if ohlc_df.empty:
        return None
    
    # Create complete date range
    all_dates = pd.date_range(start=ohlc_df['date'].min(), end=ohlc_df['date'].max(), freq='D')
    ohlc_complete = pd.DataFrame({'date': all_dates})
    
    # Merge with our OHLC data
    ohlc_complete = ohlc_complete.merge(ohlc_df, on='date', how='left')
    
    # Forward fill only the close price for missing days
    ohlc_complete['close_ffill'] = ohlc_complete['close'].ffill()
    
    # For days with no games, set OHLC to previous close (representing no price movement)
    mask_missing = ohlc_complete['open'].isna()
    ohlc_complete.loc[mask_missing, 'open'] = ohlc_complete.loc[mask_missing, 'close_ffill']
    ohlc_complete.loc[mask_missing, 'high'] = ohlc_complete.loc[mask_missing, 'close_ffill']
    ohlc_complete.loc[mask_missing, 'low'] = ohlc_complete.loc[mask_missing, 'close_ffill']
    ohlc_complete.loc[mask_missing, 'close'] = ohlc_complete.loc[mask_missing, 'close_ffill']
    
    # Create the chart
    fig = go.Figure(data=[go.Candlestick(
        x=ohlc_complete["date"],
        open=ohlc_complete["open"],
        high=ohlc_complete["high"],
        low=ohlc_complete["low"],
        close=ohlc_complete["close"],
        increasing_line_color="#089981",
        decreasing_line_color="#f23645",
        increasing_fillcolor="#089981",
        decreasing_fillcolor="#f23645"
    )])

    fig.update_layout(
        title=f"{time_control.title()} Rating History",
        plot_bgcolor="#0F0F0F",
        paper_bgcolor="#323233",
        font=dict(color="white", size=14),
        width=900,
        height=600,
        autosize=False,
        showlegend=True,
        xaxis=dict(
            showgrid=True, 
            gridcolor="rgba(242,242,242,0.06)",
            title="Date",
            rangeslider=dict(visible=False),
            tickfont=dict(size=12)
        ),
        yaxis=dict(
            showgrid=True, 
            gridcolor="rgba(242,242,242,0.06)",
            title="Rating",
            tickfont=dict(size=12)
        )
    )
    
    return fig

def fetch_and_process_games(username, time_control):
    """Fetch and process games with caching"""
    try:
        # Get user profile to validate user exists
        user_profile = get_user_profile(username)
        
        # Get list of months where user has games
        months = get_user_archives(username)
        
        if not months:
            return None, "No games found for this user."
        
        logger.info(f"Fetching {len(months)} months of data for {username}")
        
        # Prepare requests with time_class filter
        month_requests = [(username, year, month, time_control) for year, month in months]
        
        # Fetch games in parallel
        all_games = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = executor.map(get_single_month_games, month_requests)
            for result in results:
                all_games.extend(result)
        
        # Extract game data
        game_data = extract_game_data(all_games, username)
        
        if not game_data:
            return None, f"No {time_control} games found for {username}."
        
        logger.info(f"Found {len(game_data)} {time_control} games for {username}")
        
        # Create DataFrame and chart
        df = pd.DataFrame(game_data)
        fig = create_candlestick_chart(df, time_control)
        
        if fig:
            return fig, None
        else:
            return None, "Failed to create chart."
            
    except ChessAPIError as e:
        return None, str(e)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return None, "An unexpected error occurred."

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form["username"].strip()
        time_control = request.form["time_control"].strip().lower()
        
        if not validate_username(username):
            return render_template("index.html", error="Invalid username format.")
        
        if time_control not in ['bullet', 'blitz', 'rapid', 'daily']:
            return render_template("index.html", error="Invalid time control selected.")
        
        # Check cache first
        cache_key = f"{username}_{time_control}"
        cached_result = cache.get(cache_key)
        
        if cached_result:
            logger.info(f"Cache hit for {cache_key}")
            return render_template("chart.html", 
                                username=username, 
                                time_control=time_control, 
                                chart_html=cached_result)
        
        # Fetch and process
        fig, error = fetch_and_process_games(username, time_control)
        
        if error:
            return render_template("index.html", error=error)
        
        if fig:
            chart_html = pio.to_html(fig, full_html=False, config={'responsive': False})
            cache.set(cache_key, chart_html)
            return render_template("chart.html", 
                                username=username, 
                                time_control=time_control, 
                                chart_html=chart_html)
        else:
            return render_template("index.html", error="Failed to create chart.")
    
    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=False)