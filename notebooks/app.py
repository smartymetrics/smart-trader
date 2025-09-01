import streamlit as st
import asyncio
import pandas as pd
from refactored_smart_trader import (
    get_trades_with_prices,
    calculate_wallet_trade_metrics,
    get_wallet_networth_with_trades,
    HttpClient,
    helius_limiter,  # Import the shared rate limiter objects
    birdeye_limiter,
)
import os

# === Streamlit Page Configuration ===
st.set_page_config(
    page_title="Cabal Finder | Solana & EVM Trader Analytics",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# === Helper Functions with Caching ===
# Pass the rate limiter objects to the HttpClient constructor
@st.cache_data(ttl=3600)
def run_analysis_cached(token_addresses):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    async def get_data():
        # Pass the pre-configured rate limiters
        async with HttpClient(helius_limiter, birdeye_limiter) as client:
            trades_df = await get_trades_with_prices(client, token_addresses)
            return trades_df

    return loop.run_until_complete(get_data())

@st.cache_data(ttl=300)
def get_wallet_data_cached(tids, toks):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    async def get_wallet_info_async():
        # Pass the pre-configured rate limiters
        async with HttpClient(helius_limiter, birdeye_limiter) as client:
            return await get_wallet_networth_with_trades(client, tids, toks)

    return loop.run_until_complete(get_wallet_info_async())

# === Custom CSS for Unified Styling ===
st.markdown(
    """
    <style>
        /* General styling for the entire app */
        .stApp {
            background-color: #0e1117;
            color: #fafafa;
        }
        /* Style the header and navigation */
        .header-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0px 20px 0px;
            border-bottom: 1px solid #444;
        }
        .header-bar h1 {
            font-size: 2.5em;
            margin: 0;
            color: #1f77b4;
        }
        .nav-link {
            padding: 0 15px;
            text-decoration: none;
            color: white;
            font-size: 1.1em;
        }
        .nav-link.active {
            font-weight: bold;
            color: #1f77b4;
        }
        /* Styling for the main content container and its elements */
        .stContainer, [data-testid="stVerticalBlock"] > div:first-child {
            background-color: #262626 !important;
            padding: 20px;
            border-radius: 10px;
            border: 1px solid #444;
            margin-bottom: 1.5rem;
        }
        /* Custom styling for the main button */
        .stButton > button {
            background-color: #1f77b4 !important;
            color: white !important;
            border-radius: 5px;
            border: none;
            padding: 10px 20px;
            width: 100%;
            transition: background-color 0.3s ease;
        }
        .stButton > button:hover {
            background-color: #175a85 !important;
        }
        /* Style for the toggle widget */
        .st-emotion-cache-17lvw4p {
            background-color: #262626 !important;
            border-radius: 10px;
            padding: 0px !important;
        }
        .st-emotion-cache-1wv9v4q {
            color: #bbb;
        }
        /* Style for the tables/dataframes */
        .stDataFrame {
            border: 1px solid #444;
            border-radius: 10px;
            overflow: hidden;
        }
        .stDataFrame table {
            background-color: #262626 !important;
            color: #fafafa;
        }
        .stDataFrame thead th {
            background-color: #1f77b4;
            color: white;
        }
        .stDataFrame tbody tr:hover {
            background-color: #383838;
        }
        .stDataFrame th, .stDataFrame td {
            border-bottom: 1px solid #444;
            color: #fafafa; /* Ensures all text in the table is visible */
        }
    </style>
    <div class="header-bar">
        <h1>COREWIZARD</h1>
        <div>
            <a href="#" class="nav-link">Dashboard</a>
            <a href="#" class="nav-link active">Cabal Finder</a>
            <a href="#" class="nav-link">Wallet PnL</a>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

st.markdown("---")

# === Main Content Area ===
with st.container():
    st.markdown(
        """
        <div style="background-color: #262626; padding: 20px; border-radius: 10px; border: 1px solid #444;">
            <div style="display: flex; align-items: center; margin-bottom: 10px;">
                <h3 style="margin: 0; color: #1f77b4;">Cabal Finder</h3>
            </div>
            <p style="color: #bbb; font-size: 1em;">Track top traders and their activities across chains</p>
            <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                <div style="padding: 8px 15px; border-radius: 20px; background-color: #1f77b4; color: white; font-weight: bold;">Solana</div>
                <div style="padding: 8px 15px; border-radius: 20px; background-color: #333; color: #888; cursor: not-allowed;">EVM</div>
            </div>
            <h4 style="color: #fff;">Enter Token Addresses</h4>
            <p style="color: #bbb;">Enter 1-5 Solana token addresses (comma or line separated)</p>
        </div>
        """,
        unsafe_allow_html=True
    )

    token_input_string = st.text_area(
        "Token Addresses",
        "rWuzJCQNch8qwaSxqmh3kgcWnrqGmRgYPhXQP1t5ETM",
        height=100,
        label_visibility="collapsed"
    )

    if 'view_all' not in st.session_state:
        st.session_state['view_all'] = False
        
    st.session_state['view_all'] = st.toggle("Full Portfolio View")

st.markdown("---")

# === Analysis Logic ===
token_addresses_list = [addr.strip() for addr in token_input_string.replace(",", "\n").split('\n') if addr.strip()]
if len(token_addresses_list) > 5:
    st.warning("Please enter a maximum of 5 token addresses.")
    st.stop()

if st.button("Find Profitable Traders", use_container_width=True):
    if not token_addresses_list:
        st.error("Please enter at least one token address.")
    else:
        with st.spinner("Fetching and analyzing trades..."):
            trades_df = run_analysis_cached(token_addresses_list)
            
            if trades_df.empty:
                st.warning("No trades found for the given token(s).")
            else:
                st.success("Analysis complete!")
                
                trader_metrics = calculate_wallet_trade_metrics(trades_df)

                if trader_metrics:
                    trader_ids = [m['trader_id'] for m in trader_metrics]
                    
                    st.info("Fetching current wallet holdings for all traders...")
                    
                    wallet_data = get_wallet_data_cached(trader_ids, token_addresses_list)
                    
                    sorted_wallet_data = sorted(wallet_data, key=lambda x: x.get('roi', -1), reverse=True)
                    
                    wallet_info_df_list = []
                    for w_data in sorted_wallet_data:
                        wallet_info_df_list.append({
                            "Wallet Address": w_data['wallet_address'],
                            "Total Value (USD)": f"${w_data['total_value_usd']:.2f}",
                            "Realized PnL (USD)": f"${w_data['realized_pnl']:.2f}",
                            "Unrealized PnL (USD)": f"${w_data['unrealized_pnl']:.2f}",
                            "Total ROI (%)": f"{w_data['roi']:.2f}%"
                        })
                    
                    st.header("Wallet Holdings and Unrealized PnL")
                    st.dataframe(pd.DataFrame(wallet_info_df_list), use_container_width=True)

                    st.markdown("---")
                    
                    st.header("Trader PnL Metrics")
                    metrics_df = pd.DataFrame(trader_metrics)
                    st.dataframe(metrics_df, use_container_width=True)

                else:
                    st.info("No trader metrics to display.")