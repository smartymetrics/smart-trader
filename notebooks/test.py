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

logger = logging.getLogger("refactor-smart-trader")
logging.basicConfig(level=logging.INFO)

# === Configuration & Endpoints ===
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
BIRDEYE_MULTI_PRICE_URL = "https://public-api.birdeye.so/defi/multi_price"
BIRDEYE_HISTORY_URL = "https://public-api.birdeye.so/defi/history_price"

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

# === Rate limit config (doc RPS) and 60% safety margin ===
SAFETY_MULTIPLIER = 1.6  # 60% more delay

# Documented free-tier RPS (adjust if you have different plan)
DOCUMENTED_RPS = {
    "helius_tx": 10,  # Helius free (requests/sec) - conservative
    "birdeye_price": 300,  # Birdeye /defi/price
    "birdeye_multi_price": 300,  # Birdeye /defi/multi_price
    "birdeye_history": 100  # Birdeye /defi/history_price
}

def rps_to_min_interval(rps: float, safety_multiplier: float = SAFETY_MULTIPLIER) -> float:
    if rps <= 0:
        return 1.0
    return (1.0 / float(rps)) * float(safety_multiplier)

DEFAULT_RATE_LIMITS = {
    "helius_tx": rps_to_min_interval(DOCUMENTED_RPS["helius_tx"]),
    "birdeye_price": rps_to_min_interval(DOCUMENTED_RPS["birdeye_price"]),
    "birdeye_multi_price": rps_to_min_interval(DOCUMENTED_RPS["birdeye_multi_price"]),
    "birdeye_history": rps_to_min_interval(DOCUMENTED_RPS["birdeye_history"])
}

logger.info(f"Default per-endpoint min-intervals (s): {DEFAULT_RATE_LIMITS}")

# === Lightweight RateLimiter ===
class RateLimiter:
    def __init__(self, min_interval: float = 0.05):
        self.min_interval = float(min_interval)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            to_wait = self.min_interval - elapsed
            if to_wait > 0:
                await asyncio.sleep(to_wait)
            self._last_call = time.monotonic()

# === Robust async HttpClient with per-endpoint rate_key support ===
class HttpClient:
    def __init__(self, rate_limits: Optional[Dict[str, float]] = None):
        self._session: Optional[aiohttp.ClientSession] = None
        rate_limits = rate_limits or DEFAULT_RATE_LIMITS
        self._rate_limiters: Dict[str, RateLimiter] = {k: RateLimiter(v) for k, v in rate_limits.items()}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers={"User-Agent": "smart-trader/1.0"})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers={"User-Agent": "smart-trader/1.0"})
        return self._session

    async def _apply_rate_limit(self, rate_key: Optional[str]):
        if rate_key and rate_key in self._rate_limiters:
            await self._rate_limiters[rate_key].wait()

    async def _fetch(self, method: str, url: str, *, params=None, headers=None, json=None, rate_key: Optional[str] = None):
        await self._apply_rate_limit(rate_key)
        max_retries = 6
        base = 1.0
        for attempt in range(max_retries):
            try:
                async with self.session.request(method, url, params=params, headers=headers, json=json) as resp:
                    status = resp.status
                    text = await resp.text()
                    if status == 200:
                        # try parse json, fallback to text
                        try:
                            return await resp.json()
                        except Exception:
                            return text
                    # 429 -> handle Retry-After if present
                    if status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        parsed_url = url
                        if params:
                            parsed_url = f"{url}?{urllib.parse.urlencode(params)}"
                        logger.warning(f"⚠️ Rate limit 429 for URL: {parsed_url} - Retry-After: {retry_after}")
                        if retry_after:
                            try:
                                wait_secs = float(retry_after)
                                await asyncio.sleep(wait_secs + random.uniform(0, 0.5))
                            except Exception:
                                await asyncio.sleep(min(base * (2 ** attempt) + random.uniform(0, 1), 60))
                        else:
                            await asyncio.sleep(min(base * (2 ** attempt) + random.uniform(0, 1), 60))
                        continue
                    # server errors -> backoff
                    if status in (500, 502, 503, 504):
                        wait = min(base * (2 ** attempt) + random.uniform(0, 1), 60)
                        logger.warning(f"Server error {status} on {url}. Retrying in {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    # other errors -> raise
                    raise RuntimeError(f"HTTP {status} - {text}")
            except aiohttp.ClientError as e:
                wait = min(base * (2 ** attempt) + random.uniform(0, 1), 60)
                logger.warning(f"Network error {e} for {url}; retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
        raise RuntimeError(f"Max retries reached for {url}")

    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, rate_key: Optional[str] = None):
        return await self._fetch("GET", url, params=params, headers=headers, rate_key=rate_key)

    async def post_json(self, url: str, json_payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, rate_key: Optional[str] = None):
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
            # Ensure blocktime is in the correct format
            if "blocktime" in data.columns:
                data["blocktime"] = pd.to_datetime(data["blocktime"], utc=True, errors="coerce")
            
            # Remove rows where blocktime conversion failed
            data = data.dropna(subset=["blocktime"])
            return data
        
        # Fallback for old cache format (list of signatures or dicts)
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
        # Before saving, ensure all types are consistent
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

        # filter out cached
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

        # short pause to avoid bursts
        await asyncio.sleep(0.01)

    logger.info(f"✅ {len(all_trades)} new trades fetched for {address}")
    return all_trades

# === Price fetching (current + multi + historical) ===
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

async def get_current_prices_multi(client: HttpClient, mints: List[str]) -> Dict[str, float]:
    """Fetch multiple current prices from Birdeye multi_price (batched)."""
    if not mints:
        return {}
    # Try multi_price endpoint
    headers = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
    params = {"addresses": ",".join(mints), "ui_amount_mode": "raw"}
    try:
        res = await client.get_json(BIRDEYE_MULTI_PRICE_URL, params=params, headers=headers, rate_key="birdeye_multi_price")
        data = res.get("data", {}) or {}
        prices = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and v.get("value") is not None:
                    try:
                        prices[k] = float(v.get("value"))
                    except Exception:
                        prices[k] = 0.0
        if not prices and isinstance(res.get("data", {}), dict) and "items" in res.get("data", {}):
            for it in res["data"]["items"]:
                addr = it.get("address") or it.get("mint")
                val = it.get("value")
                if addr:
                    prices[addr] = float(val or 0)
        return prices
    except Exception as e:
        logger.warning(f"Multi-price call failed: {e}. Falling back to single calls.")
        out = {}
        for m in mints:
            out[m] = await get_token_current_price(client, m)
            await asyncio.sleep(DEFAULT_RATE_LIMITS.get("birdeye_price", 0.01))
        return out

async def get_token_current_price(client: HttpClient, mint: str) -> float:
    now = _now_utc()
    cached = token_price_cache.get(mint)
    if cached and (now - cached["timestamp"] < timedelta(minutes=CACHE_EXPIRY_MINUTES)):
        return float(cached["price"])
    params = {"address": mint, "ui_amount_mode": "raw"}
    headers = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
    try:
        res = await client.get_json(BIRDEYE_PRICE_URL, params=params, headers=headers, rate_key="birdeye_price")
        price = float(res.get("data", {}).get("value") or 0.0)
    except Exception as e:
        logger.error(f"Price fetch failed for {mint}: {e}")
        price = 0.0
    token_price_cache[mint] = {"price": price, "timestamp": now}
    return price

async def get_historical_price(client: HttpClient, mint: str, time_from: int, time_to: int) -> Dict[str, Any]:
    if not BIRDEYE_API_KEY:
        logger.warning("BIRDEYE_API_KEY not set; returning empty price series.")
        return {"data": {"items": []}}
    cache = load_price_cache()
    token_cache = cache.get(mint, {"prices": {}, "last_fetched_date": None})
    start_time = time_from
    if token_cache.get("last_fetched_date"):
        try:
            start_time = int(datetime.strptime(token_cache["last_fetched_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            start_time = time_from
    params = {"address": mint, "type": "30m", "time_from": start_time, "time_to": time_to}
    headers = {"X-API-KEY": BIRDEYE_API_KEY, "Accept": "application/json"}
    try:
        res = await client.get_json(BIRDEYE_HISTORY_URL, params=params, headers=headers, rate_key="birdeye_history")
        items = res.get("data", {}).get("items", []) or []
        new_prices = {int(i["unixTime"]): float(i["value"]) for i in items if "unixTime" in i and "value" in i}
        if new_prices:
            token_cache["prices"].update(new_prices)
            last_ts = max(new_prices.keys())
            token_cache["last_fetched_date"] = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            cache[mint] = token_cache
            save_price_cache(cache)
        return res
    except Exception as e:
        logger.error(f"Error fetching historical prices for {mint}: {e}")
        return {"data": {"items": []}}

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

async def get_flattened_trades(client: HttpClient, token_mints: List[str], limit: int = 100) -> pd.DataFrame:
    if len(token_mints) > 3:
        raise ValueError("Maximum of 3 token mint addresses allowed.")
    
    flattened: List[Dict[str, Any]] = []
    
    for mint in token_mints:
        cached_df = _load_df_cache(mint)
        latest_time = cached_df["blocktime"].max() if "blocktime" in cached_df.columns and not cached_df.empty else None
        
        cached_signatures = set(cached_df["signature"].dropna().unique().tolist()) if "signature" in cached_df.columns else set()
        
        txs = await get_token_trades_by_address(client, mint, transaction_type="SWAP", page_size=limit, cached_signatures=cached_signatures)
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
            
            source_token_mint = sold.get("mint") if sold.get("mint") == mint else bought.get("mint")
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
    if not trades_df.empty:
        trades_df = trades_df.astype({
            "blocktime": "datetime64[ns, UTC]",
            "trader_id": "string",
            "token_sold_mint": "string",
            "amount_sold": "float64",
            "token_bought_mint": "string",
            "amount_bought": "float64",
            "source_token": "string",
            "signature": "string"
        }, errors="ignore")
    return trades_df

# === Enrich with historical prices ===
async def get_trades_with_prices(client: HttpClient, token_mints: List[str], limit: int = 100) -> pd.DataFrame:
    trades_df = await get_flattened_trades(client, token_mints, limit)
    if trades_df.empty:
        logger.warning("No trades found.")
        return pd.DataFrame()
    
    enriched_dfs = []
    sold_mints_to_fetch = trades_df["token_sold_mint"].dropna().unique().tolist()
    prices_cache = load_price_cache()
    
    for mint in sold_mints_to_fetch:
        token_df = trades_df[trades_df["token_sold_mint"] == mint].copy()
        
        try:
            tmin = int(token_df["blocktime"].min().timestamp())
            tmax = int(token_df["blocktime"].max().timestamp())
            
            cached_prices = prices_cache.get(mint, {}).get("prices", {})
            prices_df = pd.DataFrame()
            
            if cached_prices:
                prices_df = pd.DataFrame.from_records(
                    [{"unixTime": k, "value": v} for k, v in cached_prices.items()]
                )

            new_prices_data = await get_historical_price(client, mint, tmin, tmax)
            if new_prices_data and "data" in new_prices_data and "items" in new_prices_data["data"]:
                new_prices_df = pd.DataFrame(new_prices_data["data"]["items"])
                if not new_prices_df.empty:
                    prices_df = pd.concat([prices_df, new_prices_df]).drop_duplicates(subset=["unixTime"])

            if prices_df.empty:
                token_df["price"] = None
                token_df["amount_usd"] = None
            else:
                prices_df["datetime_utc"] = pd.to_datetime(prices_df["unixTime"], unit="s", utc=True)
                prices_df.sort_values("datetime_utc", inplace=True)
                token_df.sort_values("blocktime", inplace=True)

                token_df = pd.merge_asof(
                    token_df,
                    prices_df[["datetime_utc", "value"]],
                    left_on="blocktime",
                    right_on="datetime_utc",
                    direction="nearest",
                    tolerance=pd.Timedelta("1h")
                )
                
                token_df["price"] = token_df["value"]
                token_df["amount_usd"] = token_df["amount_sold"] * token_df["price"]
                drop_cols = [c for c in ["unixTime", "datetime_utc", "value", "address"] if c in token_df.columns]
                token_df.drop(columns=drop_cols, inplace=True, errors="ignore")
        except Exception as e:
            logger.error(f"Error enriching {mint}: {e}")
            token_df["price"] = None
            token_df["amount_usd"] = None
        
        enriched_dfs.append(token_df)
        await asyncio.sleep(0.01)

    enriched_dfs = [df for df in enriched_dfs if not df.empty and not df.isna().all().all()]
    if not enriched_dfs:
        return pd.DataFrame()
    
    final = pd.concat(enriched_dfs, ignore_index=True)
    final = final.dropna(how="all", subset=["signature", "blocktime"])
    return final

# === Wallet info (RPC) ===
async def get_wallet_info(client: HttpClient, wallet_address: str) -> Dict[str, Any]:
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

# === Metrics (PNL, ROI) ===
def calculate_wallet_trade_metrics(trades_df: pd.DataFrame, wsol_mint: str = WSOL_MINT) -> List[Dict[str, Any]]:
    if trades_df.empty:
        return []
    df = trades_df.copy()
    
    def get_avg_buy_price(group_df):
        buy_trades = group_df[group_df["trade_type"] == "buy"].copy()
        if buy_trades.empty or "price" not in buy_trades.columns or "amount_bought" not in buy_trades.columns:
            return None
        buy_trades["total_usd"] = buy_trades["amount_bought"] * buy_trades["price"]
        total_usd_invested = buy_trades["total_usd"].sum()
        total_token_acquired = buy_trades["amount_bought"].sum()
        return (total_usd_invested / total_token_acquired) if total_token_acquired > 0 else 0
    
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
    
    results = []
    grouped = df.groupby(["trader_id", "token_mint"])
    
    for (trader_id, token_mint), g in grouped:
        num_trades = len(g)
        buy_volume = g[g["trade_type"]=="buy"]["amount_usd"].sum()
        sell_volume = g[g["trade_type"]=="sell"]["amount_usd"].sum()
        pnl = sell_volume - buy_volume
        roi = (pnl / buy_volume * 100) if buy_volume > 0 else None
        status = "Unrealized" if sell_volume == 0 else ("Profit" if pnl > 0 else "Loss")
        avg_buy_price = get_avg_buy_price(g)
        
        results.append({
            "trader_id": trader_id,
            "token_mint": token_mint,
            "num_trades": int(num_trades),
            "buy_volume_usd": round(float(buy_volume or 0),2),
            "sell_volume_usd": round(float(sell_volume or 0),2),
            "pnl_usd": round(float(pnl or 0),2),
            "roi_percent": round(float(roi),2) if roi is not None else None,
            "status": status,
            "avg_buy_price": avg_buy_price
        })
    
    summary = {}
    for r in results:
        tid = r["trader_id"]
        if tid not in summary:
            summary[tid] = {"trader_id": tid, "tokens_traded": 0, "tokens_in_profit": 0, "tokens_in_loss": 0, "tokens_unrealized": 0, "token_breakdown": []}
        summary[tid]["tokens_traded"] += 1
        if r["status"] == "Profit":
            summary[tid]["tokens_in_profit"] += 1
        elif r["status"] == "Loss":
            summary[tid]["tokens_in_loss"] += 1
        elif r["status"] == "Unrealized":
            summary[tid]["tokens_unrealized"] += 1
        summary[tid]["token_breakdown"].append(r)
    return list(summary.values())

# === Portfolio & Unrealized PnL ===
async def get_wallet_networth_with_trades(
    client: HttpClient,
    wallet_addresses: List[str],
    token_addresses: List[str],
    view: str = "focused",
    selected_tokens: Optional[List[str]] = None,
    trade_analysis_limit: int = 100
) -> List[Dict[str, Any]]:
    wallets = []
    trades_df = await get_trades_with_prices(client, token_addresses, trade_analysis_limit)
    trade_metrics = calculate_wallet_trade_metrics(trades_df)
    
    all_mints_to_fetch = set(token_addresses)
    if not trades_df.empty:
        all_mints_to_fetch.update(trades_df["token_bought_mint"].dropna().unique().tolist())
        all_mints_to_fetch.update(trades_df["token_sold_mint"].dropna().unique().tolist())
    
    prices_map = await get_current_prices_multi(client, list(all_mints_to_fetch))
    sol_price = prices_map.get(WSOL_MINT) or await get_token_current_price(client, WSOL_MINT)
    
    for wallet_address in wallet_addresses:
        wallet = {"wallet_address": wallet_address, "sol_balance": 0.0, "sol_value_usd": 0.0, "tokens": [], "tokens_value_usd": 0.0, "total_value_usd": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "roi": 0.0, "num_tokens_in_profit": 0}
        info = await get_wallet_info(client, wallet_address)
        sol_balance = float(info.get("sol_balance") or 0.0)
        wallet["sol_balance"] = sol_balance
        wallet["sol_value_usd"] = sol_balance * (sol_price or 0.0)
        
        tokens_iter = info.get("tokens", []) or []
        if view == "focused" and selected_tokens:
            sel = set(selected_tokens)
            tokens_iter = [t for t in tokens_iter if t.get("mint") in sel]
        
        trader_trades = [m for m in trade_metrics if m["trader_id"] == wallet_address]
        
        for t in tokens_iter:
            mint = t.get("mint")
            amount = float(t.get("uiAmount") or 0.0)
            if not mint or amount <= 0:
                continue
            
            current_price = prices_map.get(mint)
            if current_price is None:
                current_price = await get_token_current_price(client, mint)
                prices_map[mint] = current_price  # Add to cache for efficiency
                
            current_value = amount * (current_price or 0.0)
            
            avg_buy_price = next((m.get("avg_buy_price") for m in trader_trades if m.get("token_mint") == mint and m.get("avg_buy_price") is not None), None)
            
            unrealized = ((current_price - avg_buy_price) * amount) if avg_buy_price is not None and current_price is not None else 0.0
            
            wallet["tokens"].append({"mint": mint, "amount": amount, "price_usd": current_price, "value_usd": current_value, "avg_buy_price": avg_buy_price, "unrealized_pnl": unrealized})
            wallet["tokens_value_usd"] += current_value
            wallet["unrealized_pnl"] += unrealized
            if unrealized > 0:
                wallet["num_tokens_in_profit"] += 1
            await asyncio.sleep(0.005)

        realized = sum(m.get("pnl_usd", 0) for m in trader_trades)
        total_value = wallet["sol_value_usd"] + wallet["tokens_value_usd"]
        total_pnl = realized + wallet["unrealized_pnl"]
        
        total_buy_volume = sum(m.get("buy_volume_usd", 0) for m in trader_trades)
        roi = (total_pnl / total_buy_volume * 100) if total_buy_volume > 0 else 0.0
        
        wallet["realized_pnl"] = realized
        wallet["total_value_usd"] = total_value
        wallet["roi"] = roi
        wallet["trade_metrics"] = trader_trades
        wallets.append(wallet)
    
    return wallets

# === Top-level helpers ===
async def run_trade_analysis(token_addresses: List[str], limit: int = 100):
    if len(token_addresses) > 3:
        raise ValueError("Maximum of 3 token addresses allowed.")
    async with HttpClient() as client:
        df = await get_trades_with_prices(client, token_addresses, limit)
        if df.empty:
            logger.info("No trades found.")
            return []
        return calculate_wallet_trade_metrics(df)

async def analyze_traders_for_tokens(token_addresses: List[str], limit: int = 100):
    async with HttpClient() as client:
        trades_df = await get_trades_with_prices(client, token_addresses, limit=limit)
        if trades_df.empty:
            logger.info("No trades found.")
            return []
        trader_ids = trades_df["trader_id"].dropna().unique().tolist()
        wallets = []
        for tid in trader_ids:
            w = await get_wallet_networth_with_trades(client, [tid], token_addresses, view="focused", selected_tokens=token_addresses, trade_analysis_limit=limit)
            wallets.extend(w)
        return wallets

# === CLI Test ===
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", nargs="+", help="Token mint addresses (max 3)", required=False)
    parser.add_argument("--wallets", nargs="+", help="Wallet addresses to analyze", required=False)
    args = parser.parse_args()

    async def main():
        async with HttpClient(rate_limits=DEFAULT_RATE_LIMITS) as client:
            toks = args.tokens or ["rWuzJCQNch8qwaSxqmh3kgcWnrqGmRgYPhXQP1t5ETM"]
            wallets = args.wallets or []
            print("Fetching trades...")
            df = await get_trades_with_prices(client, toks, limit=100)
            print("Trades rows:", 0 if df is None else len(df.index))
            if not df.empty:
                print(df.head(3))
            
            if not wallets and not df.empty:
                trader_metrics = calculate_wallet_trade_metrics(df)
                if trader_metrics:
                    trader_id = max(trader_metrics, key=lambda x: x["num_trades"])["trader_id"]
                    print(f"\nNo wallets provided. Analyzing most active trader: {trader_id}")
                    wallets = [trader_id]
                else:
                    print("\nNo wallets or active traders to analyze.")
                    return
            
            if wallets:
                net = await get_wallet_networth_with_trades(client, wallets, toks, view="focused", selected_tokens=toks)
                for w in net:
                    print(f"\nWallet: {w['wallet_address']}")
                    print(f"Total Value (USD): ${w['total_value_usd']:.2f}")
                    print(f"Realized PnL (USD): ${w['realized_pnl']:.2f}")
                    print(f"Unrealized PnL (USD): ${w['unrealized_pnl']:.2f}")
                    print(f"Total ROI: {w['roi']:.2f}%")
                    print("--- Token Breakdown ---")
                    for trade_metric in w["trade_metrics"]:
                        print(f"  - Token: {trade_metric['token_mint']}")
                        print(f"    - Total Trades: {trade_metric['num_trades']}")
                        print(f"    - Buy Volume (USD): ${trade_metric['buy_volume_usd']:.2f}")
                        print(f"    - Sell Volume (USD): ${trade_metric['sell_volume_usd']:.2f}")
                        print(f"    - PnL (USD): ${trade_metric['pnl_usd']:.2f}")
                        print(f"    - ROI: {trade_metric['roi_percent']:.2f}%" if trade_metric['roi_percent'] is not None else "    - ROI: N/A")
                        print(f"    - Status: {trade_metric['status']}")
                    
                    if w["tokens"]:
                        print("\n--- Current Token Holdings ---")
                        for token in w["tokens"]:
                            print(f"  - Mint: {token['mint']}")
                            print(f"    - Amount: {token['amount']:.4f}")
                            print(f"    - Value: ${token['value_usd']:.2f}")
                            if token["avg_buy_price"] is not None:
                                print(f"    - Avg Buy Price: ${token['avg_buy_price']:.8f}")
                                print(f"    - Unrealized PnL: ${token['unrealized_pnl']:.2f}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Script terminated by user.")
    except Exception as e:
        logger.critical(f"An unrecoverable error occurred: {e}")


-- This is the main sccript along with main.py, i like them