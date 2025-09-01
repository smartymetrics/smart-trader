import os
import asyncio
import aiohttp
import logging
import random
import time
import urllib.parse
from typing import List, Optional, Dict, Any, Iterable, Set
from datetime import datetime, timedelta, timezone
import pandas as pd
import joblib
from dotenv import load_dotenv
from aiolimiter import AsyncLimiter

logger = logging.getLogger("refactor-smart-trader")
logging.basicConfig(level=logging.INFO)

load_dotenv()

# === Configuration & Endpoints ===
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
# BIRDEYE_MULTI_PRICE_URL = "https://public-api.birdeye.so/defi/multi_price"
# FIXED: Removed the malformed URL template - build URL dynamically
BIRDEYE_HISTORY_URL = "https://public-api.birdeye.so/defi/historical_price_unix"

CACHE_DIR = "token_trade_cache"
PRICE_CACHE_DIR = "price_cache"
PRICE_CACHE_FILE = os.path.join(PRICE_CACHE_DIR, "price_cache.joblib")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(PRICE_CACHE_DIR, exist_ok=True)

# local memory cache for quick repeated current-price lookups
token_price_cache: Dict[str, Dict[str, Any]] = {}
CACHE_EXPIRY_MINUTES = 5

WSOL_MINT = "So11111111111111111111111111111111111111112"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

# === Rate limit config ===
# Birdeye has strict rate limits - being more conservative
HELIUS_RPS_LIMIT = 9
BIRDEYE_RPS_LIMIT = 1  # Much more conservative to avoid 429 errors

# Define the rate limiters for each service
helius_limiter = AsyncLimiter(HELIUS_RPS_LIMIT, 1)
birdeye_limiter = AsyncLimiter(BIRDEYE_RPS_LIMIT, 1)

logger.info(f"Helius rate limit set to {HELIUS_RPS_LIMIT} requests per second.")
logger.info(f"Birdeye rate limit set to {BIRDEYE_RPS_LIMIT} requests per second.")

# === Utility Functions ===
def is_valid_solana_address(address: str) -> bool:
    """Validate if an address is a proper Solana address format."""
    if not address or not isinstance(address, str):
        return False
    # Solana addresses are base58 encoded and typically 32-44 characters long
    if len(address) < 32 or len(address) > 44:
        return False
    # Basic character set validation for base58
    valid_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in valid_chars for c in address)

# === Robust async HttpClient with per-endpoint rate_key support ===
class HttpClient:
    def __init__(self, helius_limiter: AsyncLimiter, birdeye_limiter: AsyncLimiter):
        self._session: Optional[aiohttp.ClientSession] = None
        self._helius_limiter = helius_limiter
        self._birdeye_limiter = birdeye_limiter

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._close_session()

    @property
    def session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers={"User-Agent": "smart-trader/1.0"})
        return self._session

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers={"User-Agent": "smart-trader/1.0"})

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _apply_rate_limit(self, rate_key: Optional[str]):
        if rate_key and "helius" in rate_key:
            await self._helius_limiter.acquire()
        elif rate_key and "birdeye" in rate_key:
            await self._birdeye_limiter.acquire()

    async def _fetch(self, method: str, url: str, *, params=None, headers=None, json=None, rate_key: Optional[str] = None):
        await self._apply_rate_limit(rate_key)
        max_retries = 5
        base = 2.0  # Slower backoff base for strict APIs

        for attempt in range(max_retries):
            try:
                async with self.session.request(method, url, params=params, headers=headers, json=json) as resp:
                    status = resp.status
                    text = await resp.text()

                    if status == 200:
                        try:
                            return await resp.json()
                        except Exception:
                            return text

                    if status == 429:  # Rate limit hit
                        retry_after = resp.headers.get("Retry-After")
                        parsed_url = url if not params else f"{url}?{urllib.parse.urlencode(params)}"
                        if retry_after:
                            wait_secs = float(retry_after) + random.uniform(0.5, 1.5)
                        else:
                            wait_secs = min(base * (2 ** attempt), 120)
                        logger.warning(f"⚠️ Rate limit 429 for URL: {parsed_url} - Retrying in {wait_secs:.1f}s")
                        await asyncio.sleep(wait_secs)
                        continue

                    if status in (400, 404) and "invalid" in text.lower():
                        raise ValueError(f"Invalid request: {text}")

                    if status in (500, 502, 503, 504):
                        wait = min(base * (2 ** attempt) + random.uniform(0, 1), 90)
                        logger.warning(f"Server error {status} on {url}. Retrying in {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue

                    raise RuntimeError(f"HTTP {status} - {text}")

            except aiohttp.ClientError as e:
                wait = min(base * (2 ** attempt) + random.uniform(0, 1), 90)
                logger.warning(f"Network error {e} for {url}; retrying in {wait:.1f}s")
                await asyncio.sleep(wait)

        raise RuntimeError(f"Max retries reached for {url}")

    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, rate_key: Optional[str] = None):
        await self._ensure_session()
        return await self._fetch("GET", url, params=params, headers=headers, rate_key=rate_key)

    async def post_json(self, url: str, json_payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, rate_key: Optional[str] = None):
        await self._ensure_session()
        return await self._fetch("POST", url, json=json_payload, headers=headers, rate_key=rate_key)

# === Caching helpers ===
def _trade_cache_path(mint: str) -> str:
    return os.path.join(CACHE_DIR, f"{mint}.pkl")

def _load_df_cache(mint: str) -> pd.DataFrame:
    path = _trade_cache_path(mint)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        data = joblib.load(path)
        if isinstance(data, pd.DataFrame):
            if "blocktime" in data.columns:
                data["blocktime"] = pd.to_datetime(data["blocktime"], utc=True, errors="coerce")
            data = data.dropna(subset=["blocktime"])
            return data
        if isinstance(data, list) and all(isinstance(d, dict) for d in data):
            df = pd.DataFrame(data)
            if "blocktime" in df.columns:
                df["blocktime"] = pd.to_datetime(df["blocktime"], unit="s", utc=True, errors="coerce")
            df = df.dropna(subset=["blocktime"])
            return df
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"Failed loading trade cache for {mint}: {e}. Returning empty DataFrame.")
        return pd.DataFrame()

def _save_df_cache(mint: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    path = _trade_cache_path(mint)
    try:
        df_to_save = df.copy()
        if "blocktime" in df_to_save.columns:
            df_to_save["blocktime"] = pd.to_datetime(df_to_save["blocktime"], utc=True, errors="coerce")
        joblib.dump(df_to_save, path)
    except Exception as e:
        logger.error(f"Failed to save DataFrame cache for {mint}: {e}")

def load_price_cache() -> Dict[str, Any]:
    if not os.path.exists(PRICE_CACHE_FILE):
        return {}
    try:
        data = joblib.load(PRICE_CACHE_FILE)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_price_cache(cache: Dict[str, Any]) -> None:
    joblib.dump(cache, PRICE_CACHE_FILE)

# === Helius trades (pagination + cached signatures) ===
async def get_token_trades_by_address(
    client: HttpClient,
    address: str,
    transaction_type: str = "SWAP",
    page_size: int = 100,
    cached_signatures: Optional[Set[str]] = None,
    max_pages: Optional[int] = None
) -> List[dict]:
    # Validate address first
    if not is_valid_solana_address(address):
        logger.error(f"Invalid Solana address format: {address}")
        return []
    
    cached_signatures = cached_signatures or set()
    all_trades: List[dict] = []
    before_sig: Optional[str] = None
    pages = 0

    while True:
        params = {
            "api-key": HELIUS_API_KEY,
            "limit": page_size,
            "type": transaction_type,
            "commitment": "finalized"
        }
        if before_sig:
            params["before"] = before_sig

        url = HELIUS_TX_URL.format(address=address)
        try:
            res = await client.get_json(url, params=params, rate_key="helius_tx")
        except Exception as e:
            logger.error(f"Error fetching trades for {address}: {e}")
            break

        if not res:
            break

        new_batch = []
        for tx in res:
            sig = tx.get("signature")
            if not sig or sig in cached_signatures:
                continue
            new_batch.append(tx)

        if not new_batch:
            break

        all_trades.extend(new_batch)
        before_sig = res[-1].get("signature")
        pages += 1
        if max_pages and pages >= max_pages:
            break

    logger.info(f"✅ {len(all_trades)} new trades fetched for {address}")
    return all_trades

# === Price fetching (current + multi + historical) ===
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# Global semaphore to control concurrency
price_semaphore = asyncio.Semaphore(1)  # Adjust 5 -> your tier limit

async def get_token_current_price(client: HttpClient, mint: str) -> float:
    """Get current price for a single token with caching and concurrency control."""
    if not is_valid_solana_address(mint):
        logger.warning(f"Invalid mint address for price lookup: {mint}")
        return 0.0
    
    # Check memory cache
    now = _now_utc()
    cached = token_price_cache.get(mint)
    if cached and (now - cached["timestamp"] < timedelta(minutes=CACHE_EXPIRY_MINUTES)):
        return float(cached["price"])
    
    headers = {"accept": "application/json", "x-chain": "solana"}
    if BIRDEYE_API_KEY:
        headers["X-API-KEY"] = BIRDEYE_API_KEY
    
    params = {"address": mint}
    async with price_semaphore:
        try:
            res = await client.get_json(
                BIRDEYE_PRICE_URL, params=params, headers=headers, rate_key="birdeye_price"
            )
            price = float(res.get("data", {}).get("value") or 0.0)
        except Exception as e:
            logger.error(f"Price fetch failed for {mint}: {e}")
            price = 0.0
    
    # Cache result
    token_price_cache[mint] = {"price": price, "timestamp": now}
    return price

from datetime import datetime, timedelta, timezone
import math

# Global in-memory cache
historical_price_cache = {}

async def get_historical_price(
    client: HttpClient, 
    mint: str, 
    time_from: int, 
    time_to: int, 
    interval: str = "30m"
) -> List[Dict[str, Any]]:
    """
    Fetch historical prices for a token in daily chunks with caching.
    time_from/time_to: Unix timestamps (seconds).
    interval: "1m", "5m", "30m", "1h", "4h", "1d"
    """
    if not is_valid_solana_address(mint):
        logger.warning(f"Invalid mint for historical price: {mint}")
        return []

    results = []
    start_dt = datetime.utcfromtimestamp(time_from).replace(tzinfo=timezone.utc)
    end_dt = datetime.utcfromtimestamp(time_to).replace(tzinfo=timezone.utc)

    # Iterate day by day
    cur_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur_dt <= end_dt:
        day_start = int(cur_dt.timestamp())
        day_end = int((cur_dt + timedelta(days=1)).timestamp())

        cache_key = f"{mint}:{day_start}:{interval}"
        if cache_key in historical_price_cache:
            day_data = historical_price_cache[cache_key]
        else:
            params = {
                "address": mint,
                "type": interval,
                "time_from": day_start,
                "time_to": day_end,
                "ui_amount_mode": "raw"
            }
            headers = {"accept": "application/json", "x-chain": "solana"}
            if BIRDEYE_API_KEY:
                headers["X-API-KEY"] = BIRDEYE_API_KEY

            try:
                res = await client.get_json(
                    BIRDEYE_HISTORICAL_PRICE_URL,
                    params=params,
                    headers=headers,
                    rate_key="birdeye_price"
                )
                day_data = res.get("data", []) or []
            except Exception as e:
                logger.error(f"Failed to fetch historical price for {mint} on {cur_dt.date()}: {e}")
                day_data = []

            historical_price_cache[cache_key] = day_data

        results.extend(day_data)
        cur_dt += timedelta(days=1)

    # Filter results to requested time range
    filtered = [p for p in results if time_from <= p.get("unixTime", 0) <= time_to]
    return filtered

def get_price_at_timestamp(historical_prices: Dict[int, float], target_timestamp: int) -> float:
    """
    Get the price closest to a specific timestamp from historical price data.
    """
    if not historical_prices:
        return 0.0
    
    # Find the closest timestamp
    closest_timestamp = min(historical_prices.keys(), 
                             key=lambda ts: abs(ts - target_timestamp))
    
    # Only use prices within 1 hour of the target time
    time_diff = abs(closest_timestamp - target_timestamp)
    if time_diff <= 3600:  # 1 hour in seconds
        return historical_prices[closest_timestamp]
    
    return 0.0

async def get_historical_prices_for_trades(client: HttpClient, trades_df: pd.DataFrame) -> Dict[str, Dict[int, float]]:
    """
    Fetch historical prices for all tokens in the trades DataFrame.
    Returns a dictionary mapping token mints to their historical price data.
    """
    if trades_df.empty:
        return {}
    
    # Get unique tokens and time range
    unique_mints = set()
    if 'token_sold_mint' in trades_df.columns:
        unique_mints.update(trades_df['token_sold_mint'].dropna().unique())
    if 'token_bought_mint' in trades_df.columns:
        unique_mints.update(trades_df['token_bought_mint'].dropna().unique())
    
    # Filter out invalid addresses
    valid_mints = [mint for mint in unique_mints if is_valid_solana_address(mint)]
    if len(valid_mints) != len(unique_mints):
        invalid_mints = [mint for mint in unique_mints if not is_valid_solana_address(mint)]
        logger.warning(f"Skipping invalid mint addresses in trades: {invalid_mints}")
    
    # Get time range from trades
    min_time = trades_df['blocktime'].min()
    max_time = trades_df['blocktime'].max()
    
    if pd.isna(min_time) or pd.isna(max_time):
        logger.warning("No valid timestamps in trades data")
        return {}
    
    time_from = int(min_time.timestamp())
    time_to = int(max_time.timestamp())
    
    logger.info(f"Fetching historical prices for {len(valid_mints)} tokens from {min_time} to {max_time}")
    
    # Fetch historical prices for all tokens
    all_historical_prices = {}
    for mint in valid_mints:
        try:
            historical_prices = await get_historical_price(client, mint, time_from, time_to)
            all_historical_prices[mint] = historical_prices
        except Exception as e:
            logger.warning(f"Failed to get historical prices for {mint}: {e}")
            all_historical_prices[mint] = {}
    
    return all_historical_prices

# === Flattening trades (safe with cache forms) ===
def _parse_token_amount(obj: Any) -> float:
    if obj is None:
        return 0.0
    if isinstance(obj, dict):
        if obj.get("uiAmount") is not None:
            try:
                return float(obj.get("uiAmount") or 0.0)
            except Exception:
                return 0.0
        amt = obj.get("amount")
        dec = obj.get("decimals")
        try:
            if amt is not None and dec is not None:
                return float(amt) / (10 ** int(dec))
        except Exception:
            return 0.0
        return 0.0
    try:
        return float(obj)
    except Exception:
        return 0.0

async def get_flattened_trades(
    client: HttpClient,
    token_mints: List[str],
    limit: int = 100
) -> pd.DataFrame:
    """
    Fetch flattened trade data for up to 3 tokens.
    Caches fetched trades and adds current prices for each token.
    """
    if len(token_mints) > 3:
        raise ValueError("Maximum of 3 token mint addresses allowed.")
    
    # Validate all addresses first
    valid_mints = [mint for mint in token_mints if is_valid_solana_address(mint)]
    if len(valid_mints) != len(token_mints):
        invalid_mints = [mint for mint in token_mints if not is_valid_solana_address(mint)]
        logger.error(f"Invalid mint addresses provided: {invalid_mints}")
        if not valid_mints:
            return pd.DataFrame()
        logger.info(f"Proceeding with valid mints: {valid_mints}")
    
    flattened: List[Dict[str, Any]] = []
    
    # Fetch trades for all valid mints
    tasks = []
    for mint in valid_mints:
        cached_df = _load_df_cache(mint)
        cached_signatures = (
            set(cached_df["signature"].dropna().unique().tolist())
            if "signature" in cached_df.columns
            else set()
        )
        tasks.append(
            get_token_trades_by_address(
                client,
                mint,
                transaction_type="SWAP",
                page_size=limit,
                cached_signatures=cached_signatures
            )
        )
    
    all_txs = await asyncio.gather(*tasks)
    
    # Build flattened DataFrame
    for mint, txs in zip(valid_mints, all_txs):
        cached_df = _load_df_cache(mint)
        latest_time = (
            cached_df["blocktime"].max()
            if "blocktime" in cached_df.columns and not cached_df.empty
            else None
        )
        
        rows = []
        for tx in txs:
            token_transfers = tx.get("tokenTransfers") or []
            if len(token_transfers) < 2:
                continue
            
            sold = token_transfers[0]
            bought = token_transfers[1]
            ts = tx.get("timestamp") or tx.get("blockTime") or tx.get("blocktime")
            if not ts:
                continue

            try:
                blocktime = pd.to_datetime(int(ts), unit="s", utc=True)
            except (ValueError, TypeError):
                continue

            if latest_time is not None and blocktime <= latest_time:
                continue
            
            source_token_mint = (
                sold.get("mint") if sold.get("mint") == mint else bought.get("mint")
            )
            if not source_token_mint:
                continue

            sold_amt = _parse_token_amount(sold.get("tokenAmount"))
            bought_amt = _parse_token_amount(bought.get("tokenAmount"))

            row = {
                "blocktime": blocktime,
                "trader_id": sold.get("fromUserAccount") or sold.get("owner") or None,
                "token_sold_mint": sold.get("mint"),
                "amount_sold": sold_amt,
                "token_bought_mint": bought.get("mint"),
                "amount_bought": bought_amt,
                "source_token": source_token_mint,
                "signature": tx.get("signature")
            }
            rows.append(row)

        new_df = pd.DataFrame(rows)
        if not new_df.empty:
            combined = pd.concat([cached_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["signature"], keep="first")
            _save_df_cache(mint, combined)
            flattened.extend(combined.to_dict("records"))
        else:
            if not cached_df.empty:
                flattened.extend(cached_df.to_dict("records"))

    trades_df = pd.DataFrame(flattened)
    if trades_df.empty:
        return trades_df

    # Ensure correct dtypes
    trades_df = trades_df.astype(
        {
            "blocktime": "datetime64[ns, UTC]",
            "trader_id": "string",
            "token_sold_mint": "string",
            "amount_sold": "float64",
            "token_bought_mint": "string",
            "amount_bought": "float64",
            "source_token": "string",
            "signature": "string"
        },
        errors="ignore"
    )

    # 🔹 Fetch current prices for all involved tokens
    unique_mints = trades_df["source_token"].dropna().unique().tolist()
    current_prices = {}
    for mint in unique_mints:
        current_prices[mint] = await get_token_current_price(client, mint)

    # Add price column
    trades_df["current_price"] = trades_df["source_token"].map(current_prices)

    return trades_df


# === Enrich with historical prices ===
async def get_trades_with_prices(
    client: HttpClient,
    token_mints: List[str],
    limit: int = 100
) -> pd.DataFrame:
    """
    Get trades with historical price data (and fallback to current price if needed).
    """
    trades_df = await get_flattened_trades(client, token_mints, limit)
    if trades_df.empty:
        logger.warning("No trades found.")
        return pd.DataFrame()
    
    # Determine time range for historical data
    min_time = int(trades_df["blocktime"].min().timestamp())
    max_time = int(trades_df["blocktime"].max().timestamp())

    # Unique tokens in trades
    unique_tokens = set(trades_df["token_sold_mint"].dropna()) | set(trades_df["token_bought_mint"].dropna())
    
    # Fetch historical prices for each token
    historical_prices_map: Dict[str, Dict[int, float]] = {}
    for mint in unique_tokens:
        try:
            historical_prices_map[mint] = await get_historical_price(client, mint, min_time, max_time)
        except Exception as e:
            logger.warning(f"Failed to get historical prices for {mint}: {e}")
            historical_prices_map[mint] = {}

    # Always fetch SOL historical prices too
    try:
        historical_prices_map[WSOL_MINT] = await get_historical_price(client, WSOL_MINT, min_time, max_time)
    except Exception as e:
        logger.warning(f"Failed to get SOL historical prices: {e}")
        historical_prices_map[WSOL_MINT] = {}

    # Helper to find price closest to timestamp
    def get_price_at_timestamp(price_dict: Dict[int, float], timestamp: int) -> float:
        if not price_dict:
            return 0.0
        closest_ts = max((ts for ts in price_dict.keys() if ts <= timestamp), default=None)
        return price_dict.get(closest_ts, 0.0) if closest_ts else 0.0

    # Apply historical prices
    def apply_price_to_trade(row):
        trade_timestamp = int(row["blocktime"].timestamp())
        
        # BUY: using SOL to buy token
        if row["token_sold_mint"] == WSOL_MINT:
            token_mint = row["token_bought_mint"]
            price = get_price_at_timestamp(historical_prices_map.get(token_mint, {}), trade_timestamp)
            sol_price = get_price_at_timestamp(historical_prices_map.get(WSOL_MINT, {}), trade_timestamp)
            row["price"] = price
            row["amount_usd"] = row["amount_sold"] * (sol_price or 1.0)
        
        # SELL: selling token for SOL
        elif row["token_bought_mint"] == WSOL_MINT:
            token_mint = row["token_sold_mint"]
            price = get_price_at_timestamp(historical_prices_map.get(token_mint, {}), trade_timestamp)
            sol_price = get_price_at_timestamp(historical_prices_map.get(WSOL_MINT, {}), trade_timestamp)
            row["price"] = price
            row["amount_usd"] = row["amount_bought"] * (sol_price or 1.0)
        
        else:
            # Token-to-token swap
            row["price"] = 0.0
            row["amount_usd"] = 0.0
        
        return row

    trades_df = trades_df.apply(apply_price_to_trade, axis=1)

    # Fallback: fetch current prices for tokens with missing historical data
    missing_price_mask = trades_df["price"] == 0.0
    if missing_price_mask.any():
        missing_tokens = trades_df[missing_price_mask].apply(
            lambda r: r["token_bought_mint"] if r["token_sold_mint"] == WSOL_MINT else r["token_sold_mint"],
            axis=1
        ).unique().tolist()
        
        current_prices = {}
        for mint in missing_tokens + [WSOL_MINT]:
            current_prices[mint] = await get_token_current_price(client, mint)
        
        def apply_fallback_price(row):
            if row["price"] == 0.0:
                token_mint = (
                    row["token_bought_mint"]
                    if row["token_sold_mint"] == WSOL_MINT
                    else row["token_sold_mint"]
                )
                row["price"] = current_prices.get(token_mint, 0.0)
                sol_price = current_prices.get(WSOL_MINT, 0.0)
                if row["token_sold_mint"] == WSOL_MINT:
                    row["amount_usd"] = row["amount_sold"] * sol_price
                elif row["token_bought_mint"] == WSOL_MINT:
                    row["amount_usd"] = row["amount_bought"] * sol_price
            return row

        trades_df = trades_df.apply(apply_fallback_price, axis=1)

    logger.info(f"Applied prices (historical + fallback) to {len(trades_df)} trades")
    return trades_df

# === FIXED Metrics (PNL, ROI) ===
async def calculate_wallet_trade_metrics(
    client: HttpClient,
    trades_df: pd.DataFrame,
    wsol_mint: str = WSOL_MINT
) -> List[Dict[str, Any]]:
    if trades_df.empty:
        return []
    
    df = trades_df.copy()

    # Classify trades as buy/sell/other
    def classify_trade(row):
        if row["token_sold_mint"] == wsol_mint:
            return "buy"
        if row["token_bought_mint"] == wsol_mint:
            return "sell"
        return "other"

    df["trade_type"] = df.apply(classify_trade, axis=1)
    df = df[df["trade_type"].isin(["buy", "sell"])].copy()

    def traded_token(row):
        return row["token_bought_mint"] if row["trade_type"] == "buy" else row["token_sold_mint"]
    
    df["token_mint"] = df.apply(traded_token, axis=1)
    
    # USD amounts (use historical amount_usd if present)
    def calculate_trade_usd_value(row):
        if row["trade_type"] == "buy":
            return row["amount_sold"] * row.get("sol_price_at_time", 1.0)
        else:
            return row["amount_bought"] * row.get("sol_price_at_time", 1.0)

    df["trade_usd_value"] = df.apply(
        lambda r: r.get("amount_usd", 0.0) if r.get("amount_usd") else calculate_trade_usd_value(r),
        axis=1
    )

    # Buy/sell token amounts
    df["buy_usd_amount"] = df.apply(lambda r: r["trade_usd_value"] if r["trade_type"] == "buy" else 0, axis=1)
    df["sell_usd_amount"] = df.apply(lambda r: r["trade_usd_value"] if r["trade_type"] == "sell" else 0, axis=1)
    df["token_bought_amount"] = df.apply(lambda r: r["amount_bought"] if r["trade_type"] == "buy" else 0, axis=1)
    df["token_sold_amount"] = df.apply(lambda r: r["amount_sold"] if r["trade_type"] == "sell" else 0, axis=1)

    # 🔹 Fetch current prices for all unique tokens one-by-one
    unique_tokens = df["token_mint"].dropna().unique().tolist()
    current_prices_map = {}
    for mint in unique_tokens:
        current_prices_map[mint] = await get_token_current_price(client, mint)
    
    # Group trades
    grouped = df.groupby(["trader_id", "token_mint"]).agg({
        'buy_usd_amount': 'sum',
        'sell_usd_amount': 'sum',
        'token_bought_amount': 'sum',
        'token_sold_amount': 'sum',
        'blocktime': ['min', 'max'],
        'signature': 'count'
    }).reset_index()

    grouped.columns = [
        'trader_id', 'token_mint', 'buy_volume_usd', 'sell_volume_usd',
        'total_token_bought', 'total_token_sold', 'first_trade', 'last_trade', 'trade_count'
    ]

    # Average buy price
    grouped["avg_buy_price"] = grouped.apply(
        lambda r: r["buy_volume_usd"] / r["total_token_bought"] if r["total_token_bought"] > 0 else 0.0, axis=1
    )

    # Holdings
    grouped["current_position"] = grouped["total_token_bought"] - grouped["total_token_sold"]

    # Realized PnL
    def calculate_realized_pnl(r):
        if r["total_token_sold"] == 0 or r["total_token_bought"] == 0:
            return 0.0
        sold_ratio = min(r["total_token_sold"] / r["total_token_bought"], 1.0)
        cost_of_sold = r["buy_volume_usd"] * sold_ratio
        return r["sell_volume_usd"] - cost_of_sold

    grouped["realized_pnl_usd"] = grouped.apply(calculate_realized_pnl, axis=1)

    # Unrealized PnL
    def calculate_unrealized_pnl(r):
        if r["current_position"] <= 0:
            return 0.0
        current_price = current_prices_map.get(r["token_mint"], 0.0)
        if current_price == 0.0 or r["avg_buy_price"] == 0.0:
            return 0.0
        return (current_price - r["avg_buy_price"]) * r["current_position"]

    grouped["unrealized_pnl_usd"] = grouped.apply(calculate_unrealized_pnl, axis=1)
    grouped["total_pnl_usd"] = grouped["realized_pnl_usd"] + grouped["unrealized_pnl_usd"]

    # ROI
    grouped["roi_percent"] = grouped.apply(
        lambda r: (r["total_pnl_usd"] / r["buy_volume_usd"] * 100) if r["buy_volume_usd"] > 0 else 0.0, axis=1
    )
    grouped["realized_roi_percent"] = grouped.apply(
        lambda r: (r["realized_pnl_usd"] / r["buy_volume_usd"] * 100) if r["buy_volume_usd"] > 0 else 0.0, axis=1
    )

    # Status
    def get_status(r):
        if r["current_position"] > 0 and r["total_token_sold"] == 0:
            return "Holding"
        elif r["current_position"] > 0 and r["total_token_sold"] > 0:
            return "Partial"
        elif r["current_position"] <= 0:
            return "Profit" if r["realized_pnl_usd"] > 0 else "Loss"
        return "Unknown"

    grouped["status"] = grouped.apply(get_status, axis=1)

    # Build summary
    summary = {}
    for _, row in grouped.iterrows():
        trader = row["trader_id"]
        if trader not in summary:
            summary[trader] = {
                "trader_id": trader,
                "tokens_in_profit": 0,
                "tokens_in_loss": 0,
                "tokens_holding": 0,
                "tokens_partial": 0,
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "total_pnl": 0.0,
                "total_buy_volume": 0.0,
                "total_sell_volume": 0.0,
                "token_breakdown": []
            }
        status = row["status"]
        if status == "Profit":
            summary[trader]["tokens_in_profit"] += 1
        elif status == "Loss":
            summary[trader]["tokens_in_loss"] += 1
        elif status == "Holding":
            summary[trader]["tokens_holding"] += 1
        elif status == "Partial":
            summary[trader]["tokens_partial"] += 1

        summary[trader]["total_realized_pnl"] += float(row["realized_pnl_usd"])
        summary[trader]["total_unrealized_pnl"] += float(row["unrealized_pnl_usd"])
        summary[trader]["total_pnl"] += float(row["total_pnl_usd"])
        summary[trader]["total_buy_volume"] += float(row["buy_volume_usd"])
        summary[trader]["total_sell_volume"] += float(row["sell_volume_usd"])

        summary[trader]["token_breakdown"].append({
            "trader_id": row["trader_id"],
            "token_mint": row["token_mint"],
            "buy_volume_usd": round(float(row["buy_volume_usd"]), 2),
            "sell_volume_usd": round(float(row["sell_volume_usd"]), 2),
            "realized_pnl_usd": round(float(row["realized_pnl_usd"]), 2),
            "unrealized_pnl_usd": round(float(row["unrealized_pnl_usd"]), 2),
            "total_pnl_usd": round(float(row["total_pnl_usd"]), 2),
            "roi_percent": round(float(row["roi_percent"]), 2),
            "realized_roi_percent": round(float(row["realized_roi_percent"]), 2),
            "avg_buy_price": round(float(row["avg_buy_price"]), 6),
            "current_position": round(float(row["current_position"]), 6),
            "current_price": current_prices_map.get(row["token_mint"], 0.0),
            "status": status,
            "trade_count": int(row["trade_count"]),
            "first_trade": row["first_trade"],
            "last_trade": row["last_trade"]
        })

    for trader_data in summary.values():
        if trader_data["total_buy_volume"] > 0:
            trader_data["overall_roi_percent"] = round(
                (trader_data["total_pnl"] / trader_data["total_buy_volume"]) * 100, 2
            )
            trader_data["overall_realized_roi_percent"] = round(
                (trader_data["total_realized_pnl"] / trader_data["total_buy_volume"]) * 100, 2
            )
        else:
            trader_data["overall_roi_percent"] = 0.0
            trader_data["overall_realized_roi_percent"] = 0.0

    return list(summary.values())


# === Portfolio & Unrealized PnL - Updated to use historical prices for trades ===
async def get_wallet_networth_with_trades(
    client: HttpClient,
    wallet_addresses: List[str],
    token_addresses: List[str],
    view: str = "focused",
    selected_tokens: Optional[List[str]] = None,
    trade_analysis_limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Calculate wallet net worth with trade analysis for given wallets.
    Uses single-token price calls.
    """
    wallets = []

    # Fetch trades and metrics
    trades_df = await get_trades_with_prices(client, token_addresses, trade_analysis_limit)
    trade_metrics = await calculate_wallet_trade_metrics(client, trades_df)

    # Build lookup: trader_id -> token_mint -> metrics
    trade_lookup = {}
    for trader in trade_metrics:
        trader_id = trader["trader_id"]
        trade_lookup[trader_id] = {}
        for token_data in trader["token_breakdown"]:
            token_mint = token_data["token_mint"]
            trade_lookup[trader_id][token_mint] = token_data

    # Collect all token mints we may need prices for
    all_mints_to_fetch = set(token_addresses)
    if not trades_df.empty:
        all_mints_to_fetch.update(trades_df["token_bought_mint"].dropna().unique().tolist())
        all_mints_to_fetch.update(trades_df["token_sold_mint"].dropna().unique().tolist())

    # 🔹 Fetch current prices one by one
    current_prices_map = {}
    for mint in all_mints_to_fetch:
        current_prices_map[mint] = await get_token_current_price(client, mint)

    # Ensure SOL price is available
    if WSOL_MINT not in current_prices_map:
        current_prices_map[WSOL_MINT] = await get_token_current_price(client, WSOL_MINT)
    sol_price = current_prices_map.get(WSOL_MINT, 0.0)

    # Fetch wallet info in parallel
    wallet_tasks = [get_wallet_info(client, addr) for addr in wallet_addresses]
    wallet_infos = await asyncio.gather(*wallet_tasks)

    for i, wallet_address in enumerate(wallet_addresses):
        wallet_info = wallet_infos[i]
        wallet = {
            "wallet_address": wallet_address,
            "sol_balance": 0.0,
            "sol_value_usd": 0.0,
            "tokens": [],
            "tokens_value_usd": 0.0,
            "total_value_usd": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "roi": 0.0,
            "num_tokens_in_profit": 0,
            "num_tokens_in_loss": 0,
            "num_tokens_holding": 0
        }

        # SOL balance & value
        sol_balance = float(wallet_info.get("sol_balance") or 0.0)
        wallet["sol_balance"] = sol_balance
        wallet["sol_value_usd"] = sol_balance * sol_price

        # Token holdings
        tokens_iter = wallet_info.get("tokens", []) or []
        if view == "focused" and selected_tokens:
            sel = set(selected_tokens)
            tokens_iter = [t for t in tokens_iter if t.get("mint") in sel]

        trader_trade_data = trade_lookup.get(wallet_address, {})

        # Fetch prices for any missing tokens in portfolio
        missing_mints = [
            t.get("mint")
            for t in tokens_iter
            if t.get("mint") and t.get("mint") not in current_prices_map
        ]
        for mint in missing_mints:
            current_prices_map[mint] = await get_token_current_price(client, mint)

        # Process each token holding
        for t in tokens_iter:
            mint = t.get("mint")
            amount = float(t.get("uiAmount") or 0.0)
            if not mint or amount <= 0:
                continue

            current_price = current_prices_map.get(mint, 0.0)
            current_value = amount * current_price
            token_trade_data = trader_trade_data.get(mint, {})
            avg_buy_price = token_trade_data.get("avg_buy_price", 0.0)

            # Unrealized PnL
            unrealized_pnl = 0.0
            if avg_buy_price > 0 and current_price > 0:
                unrealized_pnl = (current_price - avg_buy_price) * amount

            token_info = {
                "mint": mint,
                "amount": amount,
                "price_usd": current_price,
                "value_usd": current_value,
                "avg_buy_price": avg_buy_price,
                "unrealized_pnl": unrealized_pnl,
                "trade_data": token_trade_data
            }

            wallet["tokens"].append(token_info)
            wallet["tokens_value_usd"] += current_value
            wallet["unrealized_pnl"] += unrealized_pnl

            # Profit/Loss counters
            if unrealized_pnl > 0:
                wallet["num_tokens_in_profit"] += 1
            elif unrealized_pnl < 0:
                wallet["num_tokens_in_loss"] += 1
            else:
                wallet["num_tokens_holding"] += 1

        # Add realized PnL and totals
        trader_metrics = next((m for m in trade_metrics if m["trader_id"] == wallet_address), None)
        if trader_metrics:
            wallet["realized_pnl"] = trader_metrics.get("total_realized_pnl", 0.0)
            wallet["total_buy_volume"] = trader_metrics.get("total_buy_volume", 0.0)
            wallet["total_sell_volume"] = trader_metrics.get("total_sell_volume", 0.0)

        wallet["total_value_usd"] = wallet["sol_value_usd"] + wallet["tokens_value_usd"]
        wallet["total_pnl"] = wallet["realized_pnl"] + wallet["unrealized_pnl"]

        total_invested = trader_metrics.get("total_buy_volume", 0.0) if trader_metrics else 0.0
        wallet["roi"] = (wallet["total_pnl"] / total_invested * 100) if total_invested > 0 else 0.0
        wallet["trade_metrics"] = trader_metrics.get("token_breakdown", []) if trader_metrics else []

        wallets.append(wallet)

    return wallets

# === Wallet info (RPC) ===
async def get_wallet_info(client: HttpClient, wallet_address: str) -> Dict[str, Any]:
    if not is_valid_solana_address(wallet_address):
        logger.error(f"Invalid wallet address format: {wallet_address}")
        return {"sol_balance": None, "tokens": []}
    
    wallet = {"sol_balance": None, "tokens": []}
    headers = {"Content-Type": "application/json"}
    sol_payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet_address]}
    
    try:
        sol_res = await client.post_json(HELIUS_RPC_URL, sol_payload, headers=headers, rate_key="helius_tx")
        lamports = sol_res.get("result", {}).get("value")
        if lamports is not None:
            wallet["sol_balance"] = lamports / 1_000_000_000
    except Exception as e:
        logger.error(f"Error fetching SOL balance for {wallet_address}: {e}")
        
    tokens_payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getTokenAccountsByOwner",
        "params": [wallet_address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
    }
    
    try:
        tok_res = await client.post_json(HELIUS_RPC_URL, tokens_payload, headers=headers, rate_key="helius_tx")
        accounts = tok_res.get("result", {}).get("value", []) or []
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amount_obj = info.get("tokenAmount", {}) or {}
            wallet["tokens"].append({
                "mint": info.get("mint"),
                "uiAmount": amount_obj.get("uiAmount"),
                "decimals": amount_obj.get("decimals")
            })
    except Exception as e:
        logger.error(f"Error fetching tokens for {wallet_address}: {e}")
    return wallet

# === UPDATED Top-level helpers ===
async def run_trade_analysis(token_addresses: List[str], limit: int = 100):
    if len(token_addresses) > 3:
        raise ValueError("Maximum of 3 token addresses allowed.")
    
    # Validate addresses
    valid_addresses = [addr for addr in token_addresses if is_valid_solana_address(addr)]
    if len(valid_addresses) != len(token_addresses):
        invalid_addresses = [addr for addr in token_addresses if not is_valid_solana_address(addr)]
        logger.error(f"Invalid token addresses: {invalid_addresses}")
        if not valid_addresses:
            return []
    
    async with HttpClient(helius_limiter, birdeye_limiter) as client:
        df = await get_trades_with_prices(client, valid_addresses, limit)
        if df.empty:
            logger.info("No trades found.")
            return []
        return await calculate_wallet_trade_metrics(client, df)

async def analyze_traders_for_tokens(token_addresses: List[str], limit: int = 100):
    # Validate addresses
    valid_addresses = [addr for addr in token_addresses if is_valid_solana_address(addr)]
    if len(valid_addresses) != len(token_addresses):
        invalid_addresses = [addr for addr in token_addresses if not is_valid_solana_address(addr)]
        logger.error(f"Invalid token addresses: {invalid_addresses}")
        if not valid_addresses:
            return []
    
    async with HttpClient(helius_limiter, birdeye_limiter) as client:
        trades_df = await get_trades_with_prices(client, valid_addresses, limit=limit)
        if trades_df.empty:
            logger.info("No trades found.")
            return []
        trader_ids = trades_df["trader_id"].dropna().unique().tolist()
        wallets = []
        for tid in trader_ids:
            w = await get_wallet_networth_with_trades(client, [tid], valid_addresses, view="focused", selected_tokens=valid_addresses, trade_analysis_limit=limit)
            wallets.extend(w)
        return wallets

async def calculate_wallet_trade_metrics(
    client: HttpClient, 
    trades_df: pd.DataFrame, 
    wsol_mint: str = WSOL_MINT
) -> List[Dict[str, Any]]:
    if trades_df.empty:
        return []

    df = trades_df.copy()  # Avoid SettingWithCopyWarning

    # Classify trades
    def classify_trade(row):
        if row["token_sold_mint"] == wsol_mint:
            return "buy"
        if row["token_bought_mint"] == wsol_mint:
            return "sell"
        return "other"

    df["trade_type"] = df.apply(classify_trade, axis=1)
    df = df[df["trade_type"].isin(["buy", "sell"])].copy()

    def traded_token(row):
        return row["token_bought_mint"] if row["trade_type"] == "buy" else row["token_sold_mint"]

    df["token_mint"] = df.apply(traded_token, axis=1)

    # Calculate trade USD values
    def calculate_trade_usd_value(row):
        if row["trade_type"] == "buy":
            return row["amount_sold"] * row.get("sol_price_at_time", 1.0)
        else:
            return row["amount_bought"] * row.get("sol_price_at_time", 1.0)

    df["trade_usd_value"] = df.apply(
        lambda row: row.get("amount_usd", 0.0) if row.get("amount_usd") else calculate_trade_usd_value(row),
        axis=1
    )

    # Separate buy/sell amounts
    df["buy_usd_amount"] = df.apply(lambda row: row["trade_usd_value"] if row["trade_type"] == "buy" else 0, axis=1)
    df["sell_usd_amount"] = df.apply(lambda row: row["trade_usd_value"] if row["trade_type"] == "sell" else 0, axis=1)
    df["token_bought_amount"] = df.apply(lambda row: row["amount_bought"] if row["trade_type"] == "buy" else 0, axis=1)
    df["token_sold_amount"] = df.apply(lambda row: row["amount_sold"] if row["trade_type"] == "sell" else 0, axis=1)

    # 🔹 Get current prices with caching
    unique_tokens = df["token_mint"].dropna().unique().tolist()
    current_prices_map = {}
    now = _now_utc()

    for token in unique_tokens:
        cached = token_price_cache.get(token)
        if cached and (now - cached["timestamp"] < timedelta(minutes=CACHE_EXPIRY_MINUTES)):
            current_prices_map[token] = cached["price"]
        else:
            price = await get_token_current_price(client, token)
            current_prices_map[token] = price
            token_price_cache[token] = {"price": price, "timestamp": now}

    # Group by trader and token
    grouped = df.groupby(["trader_id", "token_mint"]).agg({
        'buy_usd_amount': 'sum',
        'sell_usd_amount': 'sum',
        'token_bought_amount': 'sum',
        'token_sold_amount': 'sum',
        'blocktime': ['min', 'max'],
        'signature': 'count'
    }).reset_index()

    grouped.columns = [
        'trader_id', 'token_mint', 'buy_volume_usd', 'sell_volume_usd',
        'total_token_bought', 'total_token_sold', 'first_trade', 'last_trade', 'trade_count'
    ]

    # Compute metrics
    grouped["avg_buy_price"] = grouped.apply(
        lambda row: row["buy_volume_usd"] / row["total_token_bought"] if row["total_token_bought"] > 0 else 0.0,
        axis=1
    )
    grouped["current_position"] = grouped["total_token_bought"] - grouped["total_token_sold"]

    def calculate_realized_pnl(row):
        if row["total_token_sold"] == 0 or row["total_token_bought"] == 0:
            return 0.0
        sold_ratio = min(row["total_token_sold"] / row["total_token_bought"], 1.0)
        cost_of_sold = row["buy_volume_usd"] * sold_ratio
        return row["sell_volume_usd"] - cost_of_sold

    grouped["realized_pnl_usd"] = grouped.apply(calculate_realized_pnl, axis=1)

    def calculate_unrealized_pnl(row):
        if row["current_position"] <= 0:
            return 0.0
        current_price = current_prices_map.get(row["token_mint"], 0.0)
        return (current_price - row["avg_buy_price"]) * row["current_position"]

    grouped["unrealized_pnl_usd"] = grouped.apply(calculate_unrealized_pnl, axis=1)
    grouped["total_pnl_usd"] = grouped["realized_pnl_usd"] + grouped["unrealized_pnl_usd"]
    grouped["roi_percent"] = grouped.apply(
        lambda row: (row["total_pnl_usd"] / row["buy_volume_usd"] * 100) if row["buy_volume_usd"] > 0 else 0.0,
        axis=1
    )
    grouped["realized_roi_percent"] = grouped.apply(
        lambda row: (row["realized_pnl_usd"] / row["buy_volume_usd"] * 100) if row["buy_volume_usd"] > 0 else 0.0,
        axis=1
    )

    def get_status(row):
        if row["current_position"] > 0 and row["total_token_sold"] == 0:
            return "Holding"
        elif row["current_position"] > 0 and row["total_token_sold"] > 0:
            return "Partial"
        elif row["current_position"] <= 0:
            return "Profit" if row["realized_pnl_usd"] > 0 else "Loss"
        return "Unknown"

    grouped["status"] = grouped.apply(get_status, axis=1)

    # Summarize per trader
    summary = {}
    for _, row in grouped.iterrows():
        trader_id = row["trader_id"]
        if trader_id not in summary:
            summary[trader_id] = {
                "trader_id": trader_id,
                "tokens_in_profit": 0,
                "tokens_in_loss": 0,
                "tokens_holding": 0,
                "tokens_partial": 0,
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "total_pnl": 0.0,
                "total_buy_volume": 0.0,
                "total_sell_volume": 0.0,
                "token_breakdown": []
            }

        status = row["status"]
        if status == "Profit":
            summary[trader_id]["tokens_in_profit"] += 1
        elif status == "Loss":
            summary[trader_id]["tokens_in_loss"] += 1
        elif status == "Holding":
            summary[trader_id]["tokens_holding"] += 1
        elif status == "Partial":
            summary[trader_id]["tokens_partial"] += 1

        summary[trader_id]["total_realized_pnl"] += float(row["realized_pnl_usd"] or 0)
        summary[trader_id]["total_unrealized_pnl"] += float(row["unrealized_pnl_usd"] or 0)
        summary[trader_id]["total_pnl"] += float(row["total_pnl_usd"] or 0)
        summary[trader_id]["total_buy_volume"] += float(row["buy_volume_usd"] or 0)
        summary[trader_id]["total_sell_volume"] += float(row["sell_volume_usd"] or 0)

        summary[trader_id]["token_breakdown"].append({
            "trader_id": row["trader_id"],
            "token_mint": row["token_mint"],
            "buy_volume_usd": round(float(row["buy_volume_usd"] or 0), 2),
            "sell_volume_usd": round(float(row["sell_volume_usd"] or 0), 2),
            "realized_pnl_usd": round(float(row["realized_pnl_usd"] or 0), 2),
            "unrealized_pnl_usd": round(float(row["unrealized_pnl_usd"] or 0), 2),
            "total_pnl_usd": round(float(row["total_pnl_usd"] or 0), 2),
            "roi_percent": round(float(row["roi_percent"]), 2),
            "realized_roi_percent": round(float(row["realized_roi_percent"]), 2),
            "avg_buy_price": round(float(row["avg_buy_price"]), 6),
            "current_position": round(float(row["current_position"]), 6),
            "current_price": current_prices_map.get(row["token_mint"], 0.0),
            "status": status,
            "trade_count": int(row["trade_count"]),
            "first_trade": row["first_trade"],
            "last_trade": row["last_trade"]
        })

    for trader_data in summary.values():
        if trader_data["total_buy_volume"] > 0:
            trader_data["overall_roi_percent"] = round(
                (trader_data["total_pnl"] / trader_data["total_buy_volume"]) * 100, 2
            )
            trader_data["overall_realized_roi_percent"] = round(
                (trader_data["total_realized_pnl"] / trader_data["total_buy_volume"]) * 100, 2
            )
        else:
            trader_data["overall_roi_percent"] = 0.0
            trader_data["overall_realized_roi_percent"] = 0.0

    return list(summary.values())

# === CLI Test ===
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", nargs="+", help="Token mint addresses (max 3)", required=False)
    parser.add_argument("--wallets", nargs="+", help="Wallet addresses to analyze", required=False)
    args = parser.parse_args()

    async def main():
        # Use a known valid Solana token address as default
        toks = args.tokens or ["6fzQtvZ224efM1Mai7avfLWke59ipLfdWaJ21UdYpump"]  # USDC on Solana
        
        # Validate the provided token addresses
        print(f"Analyzing tokens: {toks}")
        for tok in toks:
            if not is_valid_solana_address(tok):
                print(f"WARNING: {tok} appears to be an invalid Solana address")
        
        if args.wallets:
            # Validate wallet addresses
            valid_wallets = [w for w in args.wallets if is_valid_solana_address(w)]
            if len(valid_wallets) != len(args.wallets):
                invalid_wallets = [w for w in args.wallets if not is_valid_solana_address(w)]
                print(f"WARNING: Invalid wallet addresses: {invalid_wallets}")
            
            if valid_wallets:
                wallets = await analyze_traders_for_tokens(toks)
                for w in wallets:
                    print(f"Wallet: {w}")
            else:
                print("No valid wallet addresses provided")
        else:
            try:
                trade_metrics = await run_trade_analysis(toks)
                if trade_metrics:
                    print(f"Found {len(trade_metrics)} traders")
                    for m in trade_metrics:
                        print(f"Trader: {m}")
                else:
                    print("No trade data found for the provided tokens")
            except Exception as e:
                print(f"Error during analysis: {e}")
                logger.exception("Full error traceback:")
    
    asyncio.run(main())