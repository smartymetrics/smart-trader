import streamlit as st
import asyncio
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
from datetime import datetime, timedelta
import json

# Import your trading script functions
# Make sure the script is saved as 'refactored_smart_trader.py' or update the import name
try:
    from refactored_smart_trader import (
        HttpClient, helius_limiter, birdeye_limiter,
        run_trade_analysis, analyze_traders_for_tokens,
        get_wallet_networth_with_trades, get_trades_with_prices,
        calculate_wallet_trade_metrics, WSOL_MINT
    )
    SCRIPT_AVAILABLE = True
except ImportError:
    st.error("⚠️ Could not import smart_trader module. Please ensure refactored_smart_trader.py is in the same directory.")
    SCRIPT_AVAILABLE = False

# === Streamlit App Configuration ===
st.set_page_config(
    page_title="Smart Trader Analytics",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# === CSS Styling ===
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
        margin: 0.5rem 0;
    }
    .profit-card {
        background: linear-gradient(135deg, #56ab2f 0%, #a8e6cf 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
    }
    .loss-card {
        background: linear-gradient(135deg, #ff416c 0%, #ff4b2b 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
    }
    .trader-card {
        border: 1px solid #e0e0e0;
        padding: 1rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        background: #f8f9fa;
    }
</style>
""", unsafe_allow_html=True)

# === Helper Functions ===
def format_usd(value):
    """Format USD values with appropriate suffixes"""
    if pd.isna(value) or value == 0:
        return "$0"
    if abs(value) >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif abs(value) >= 1_000:
        return f"${value/1_000:.2f}K"
    else:
        return f"${value:.2f}"

def format_percentage(value):
    """Format percentage values"""
    if pd.isna(value):
        return "N/A"
    return f"{value:.2f}%"

def truncate_address(address, start=6, end=4):
    """Truncate wallet/token addresses for display"""
    if not address or len(address) <= start + end:
        return address
    return f"{address[:start]}...{address[-end:]}"

# === Async function runner ===
# === Async function runner ===
def run_async(coro):
    """Safely run async functions in Streamlit without multiple event loops"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # No loop running
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    else:
        # If already running (Streamlit), wrap in task
        return asyncio.run_coroutine_threadsafe(coro, loop).result()


# === Data Processing Functions ===
def process_trader_metrics(results):
    """Process trader metrics from the updated script format"""
    trader_summary = []
    
    for trader in results:
        trader_id = trader.get('trader_id', '')
        token_breakdown = trader.get('token_breakdown', [])
        
        total_buy_volume = trader.get('total_buy_volume', 0)
        total_sell_volume = trader.get('total_sell_volume', 0)
        total_realized_pnl = trader.get('total_realized_pnl', 0)
        overall_roi = trader.get('overall_roi_percent', 0)
        
        tokens_in_profit = trader.get('tokens_in_profit', 0)
        tokens_in_loss = trader.get('tokens_in_loss', 0)
        tokens_holding = trader.get('tokens_holding', 0)
        tokens_partial = trader.get('tokens_partial', 0)
        
        # Calculate unrealized PnL
        unrealized_pnl = sum(
            token.get('realized_pnl_usd', 0) for token in token_breakdown 
            if token.get('status') in ['Holding', 'Partial']
        )
        total_pnl = total_realized_pnl + unrealized_pnl

        # ✅ Force ROI & PnL = 0 if no sell volume
        if total_sell_volume == 0:
            total_realized_pnl = 0
            unrealized_pnl = 0
            total_pnl = 0
            overall_roi = 0

        trader_summary.append({
            # 'Trader ID': truncate_address(trader_id),
            'Trader ID': trader_id,
            'Total Buy Volume': total_buy_volume,
            'Total Sell Volume': total_sell_volume,
            'Realized PnL': total_realized_pnl,
            'Unrealized PnL': unrealized_pnl,
            'Total PnL': total_pnl,
            'ROI (%)': overall_roi,
            'Tokens in Profit': tokens_in_profit,
            'Tokens in Loss': tokens_in_loss,
            'Tokens Holding': tokens_holding,
            'Tokens Partial': tokens_partial,
            'Total Positions': len(token_breakdown)
        })
    
    return trader_summary

# === Main App ===
def main():
    st.title("📈 Smart Trader Analytics Dashboard")
    st.markdown("---")
    
    if not SCRIPT_AVAILABLE:
        st.stop()
    
    # === Sidebar Configuration ===
    st.sidebar.header("🔧 Configuration")
    
    analysis_type = st.sidebar.selectbox(
        "Analysis Type",
        ["Token Trade Analysis", "Trader Portfolio Analysis", "Wallet Analysis"]
    )
    
    # === Token Input Section ===
    st.sidebar.subheader("🪙 Token Configuration")
    token_input_method = st.sidebar.radio(
        "Token Input Method",
        ["Manual Entry", "Predefined Examples"]
    )
    
    if token_input_method == "Predefined Examples":
        example_tokens = {
            "Example Token 1": "GNKpYyQkVsvkPYTMsnBDZNZzLA8FZmiWazHhTX9jNyhS",
            "Solana USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Solana USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        }
        selected_example = st.sidebar.selectbox("Select Example", list(example_tokens.keys()))
        token_addresses = [example_tokens[selected_example]]
    else:
        token_addresses_input = st.sidebar.text_area(
            "Token Mint Addresses (one per line, max 3)",
            placeholder="Enter token mint addresses...",
            height=100
        )
        token_addresses = [addr.strip() for addr in token_addresses_input.split('\n') if addr.strip()]
    
    if len(token_addresses) > 3:
        st.sidebar.error("⚠️ Maximum of 3 token addresses allowed")
        st.stop()
    
    # === Wallet Input Section (for wallet analysis) ===
    wallet_addresses = []
    if analysis_type in ["Trader Portfolio Analysis", "Wallet Analysis"]:
        st.sidebar.subheader("👛 Wallet Configuration")
        wallet_input = st.sidebar.text_area(
            "Wallet Addresses (one per line)",
            placeholder="Enter wallet addresses...",
            height=100
        )
        wallet_addresses = [addr.strip() for addr in wallet_input.split('\n') if addr.strip()]
    
    # === Analysis Parameters ===
    st.sidebar.subheader("⚙️ Parameters")
    trade_limit = st.sidebar.slider("Trade Limit", min_value=50, max_value=500, value=50, step=20)
    
    # === API Limitations Notice ===
    st.sidebar.info("ℹ️ Trade Limit helps the app to run better, the larger the limit the faster it would run, it get most recent transactions and goes in batches to past. Preferable to use small limit to avoid API limit rate")
    
    # === Run Analysis Button ===
    if st.sidebar.button("🚀 Run Analysis", type="primary"):
        if not token_addresses:
            st.error("Please enter at least one token address")
            st.stop()
        
        if analysis_type in ["Trader Portfolio Analysis", "Wallet Analysis"] and not wallet_addresses:
            st.error("Please enter at least one wallet address for this analysis type")
            st.stop()
        
        # === Progress and Analysis Execution ===
        with st.spinner("🔄 Fetching and analyzing data..."):
            try:
                if analysis_type == "Token Trade Analysis":
                    results = run_async(run_trade_analysis(token_addresses, trade_limit))
                    st.session_state.last_analysis_data = results
                    display_token_trade_analysis(results, token_addresses)
                
                elif analysis_type == "Trader Portfolio Analysis":
                    results = run_async(analyze_traders_for_tokens(token_addresses, trade_limit))
                    st.session_state.last_analysis_data = results
                    display_trader_portfolio_analysis(results, token_addresses)
                
                elif analysis_type == "Wallet Analysis":
                    async def wallet_analysis():
                        async with HttpClient(helius_limiter, birdeye_limiter) as client:
                            return await get_wallet_networth_with_trades(
                                client, wallet_addresses, token_addresses, 
                                trade_analysis_limit=trade_limit
                            )
                    results = run_async(wallet_analysis())
                    st.session_state.last_analysis_data = results
                    display_wallet_analysis(results, token_addresses)
                    
                st.session_state.analysis_complete = True
                    
            except Exception as e:
                st.error(f"❌ Analysis failed: {str(e)}")
                st.exception(e)

def display_token_trade_analysis(results, token_addresses):
    """Display token trade analysis results with updated data structure"""
    st.header("🪙 Token Trade Analysis")
    
    if not results:
        st.warning("No trading data found for the specified tokens.")
        return
    
    # Process the results using the new data structure
    trader_summary = process_trader_metrics(results)
    
    if not trader_summary:
        st.warning("No valid trader data to display.")
        return
    
    # === Calculate aggregate metrics ===
    total_traders = len(trader_summary)
    total_buy_volume = sum(t['Total Buy Volume'] for t in trader_summary)
    total_sell_volume = sum(t['Total Sell Volume'] for t in trader_summary)
    total_realized_pnl = sum(t['Realized PnL'] for t in trader_summary)
    total_unrealized_pnl = sum(t['Unrealized PnL'] for t in trader_summary)
    total_pnl = sum(t['Total PnL'] for t in trader_summary)
    total_tokens_in_profit = sum(t['Tokens in Profit'] for t in trader_summary)
    
    # Calculate overall ROI properly
    overall_roi = (total_pnl / total_buy_volume * 100) if total_buy_volume > 0 else 0
    avg_tokens_in_profit = total_tokens_in_profit / total_traders if total_traders > 0 else 0
    
    # === Key Metrics Overview ===
    st.subheader("📊 Key Portfolio Metrics")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.markdown(f"""
        <div class="metric-card">
            <h4>💰 Total Investment</h4>
            <h3>{format_usd(total_buy_volume)}</h3>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        pnl_class = "profit-card" if total_pnl > 0 else "loss-card"
        st.markdown(f"""
        <div class="{pnl_class}">
            <h4>📈 Total PnL</h4>
            <h3>{format_usd(total_pnl)}</h3>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        realized_class = "profit-card" if total_realized_pnl > 0 else "loss-card"
        st.markdown(f"""
        <div class="{realized_class}">
            <h4>✅ Realized PnL</h4>
            <h3>{format_usd(total_realized_pnl)}</h3>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        unrealized_class = "profit-card" if total_unrealized_pnl > 0 else "loss-card"
        st.markdown(f"""
        <div class="{unrealized_class}">
            <h4>⏳ Unrealized PnL</h4>
            <h3>{format_usd(total_unrealized_pnl)}</h3>
        </div>
        """, unsafe_allow_html=True)
    
    with col5:
        roi_class = "profit-card" if overall_roi > 0 else "loss-card"
        st.markdown(f"""
        <div class="{roi_class}">
            <h4>🎯 Overall ROI</h4>
            <h3>{format_percentage(overall_roi)}</h3>
        </div>
        """, unsafe_allow_html=True)
    
    # === Secondary Metrics ===
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Traders", total_traders)
    with col2:
        st.metric("Avg Tokens in Profit", f"{avg_tokens_in_profit:.1f}")
    with col3:
        st.metric("Total Buy Volume", format_usd(total_buy_volume))
    with col4:
        st.metric("Total Sell Volume", format_usd(total_sell_volume))
    
    # === Detailed Trader Table ===
    st.subheader("📋 Individual Trader Performance")
    
    trader_df = pd.DataFrame(trader_summary)
    
    # Format the dataframe for better display
    display_df = trader_df.copy()
    for col in ['Total Buy Volume', 'Total Sell Volume', 'Realized PnL', 'Unrealized PnL', 'Total PnL']:
        if col in display_df.columns:
            display_df[col] = display_df[col].apply(format_usd)
    
    if 'ROI (%)' in display_df.columns:
        display_df['ROI (%)'] = display_df['ROI (%)'].apply(format_percentage)
    
    st.dataframe(display_df, use_container_width=True)
    
    # === Visualizations ===
    if len(trader_summary) > 0:
        st.subheader("📊 Performance Analytics")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Total PnL Distribution
            fig_pnl = px.histogram(
                trader_df, 
                x='Total PnL', 
                title="Total PnL Distribution Across Traders",
                color_discrete_sequence=['#667eea'],
                nbins=min(20, len(trader_df))
            )
            fig_pnl.update_layout(xaxis_title="Total PnL (USD)", yaxis_title="Number of Traders")
            st.plotly_chart(fig_pnl, use_container_width=True)
        
        with col2:
            # ROI vs Tokens in Profit
            fig_roi = px.scatter(
                trader_df, 
                x='Tokens in Profit', 
                y='ROI (%)',
                size='Total Buy Volume',
                title="ROI vs Number of Profitable Tokens",
                color='Total PnL',
                color_continuous_scale='RdYlGn',
                hover_data=['Trader ID', 'Total Buy Volume']
            )
            fig_roi.update_layout(xaxis_title="Number of Tokens in Profit", yaxis_title="ROI (%)")
            st.plotly_chart(fig_roi, use_container_width=True)
        
        # === Token Status Distribution ===
        col1, col2 = st.columns(2)
        
        with col1:
            # Realized vs Unrealized PnL
            pnl_comparison = pd.DataFrame({
                'PnL Type': ['Realized', 'Unrealized'],
                'Amount': [total_realized_pnl, total_unrealized_pnl]
            })
            
            fig_pnl_comp = px.bar(
                pnl_comparison,
                x='PnL Type',
                y='Amount',
                title="Realized vs Unrealized PnL",
                color='PnL Type',
                color_discrete_map={'Realized': '#56ab2f', 'Unrealized': '#ff9500'}
            )
            fig_pnl_comp.update_layout(yaxis_title="PnL (USD)", showlegend=False)
            st.plotly_chart(fig_pnl_comp, use_container_width=True)
        
        with col2:
            # Token Status Distribution
            status_data = []
            for trader in trader_summary:
                status_data.append({
                    'Status': 'Profit',
                    'Count': trader['Tokens in Profit']
                })
                status_data.append({
                    'Status': 'Loss',
                    'Count': trader['Tokens in Loss']
                })
                status_data.append({
                    'Status': 'Holding',
                    'Count': trader['Tokens Holding']
                })
                status_data.append({
                    'Status': 'Partial',
                    'Count': trader['Tokens Partial']
                })
            
            if status_data:
                status_df = pd.DataFrame(status_data).groupby('Status')['Count'].sum().reset_index()
                fig_status = px.pie(
                    status_df,
                    values='Count',
                    names='Status',
                    title="Token Position Status Distribution",
                    color_discrete_map={
                        'Profit': '#56ab2f',
                        'Loss': '#ff416c',
                        'Holding': '#667eea',
                        'Partial': '#ff9500'
                    }
                )
                st.plotly_chart(fig_status, use_container_width=True)
    
    # === Top Performers Section ===
    st.subheader("🏆 Top Performers")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.write("**Top 5 by Total PnL:**")
        top_pnl = trader_df.nlargest(5, 'Total PnL')[['Trader ID', 'Total PnL']].copy()
        top_pnl['Total PnL'] = top_pnl['Total PnL'].apply(format_usd)
        st.dataframe(top_pnl, hide_index=True, use_container_width=True)
    
    with col2:
        st.write("**Top 5 by ROI:**")
        top_roi = trader_df.nlargest(5, 'ROI (%)')[['Trader ID', 'ROI (%)']].copy()
        top_roi['ROI (%)'] = top_roi['ROI (%)'].apply(format_percentage)
        st.dataframe(top_roi, hide_index=True, use_container_width=True)
    
    with col3:
        st.write("**Top 5 by Buy Volume:**")
        top_volume = trader_df.nlargest(5, 'Total Buy Volume')[['Trader ID', 'Total Buy Volume']].copy()
        top_volume['Total Buy Volume'] = top_volume['Total Buy Volume'].apply(format_usd)
        st.dataframe(top_volume, hide_index=True, use_container_width=True)

def display_trader_portfolio_analysis(results, token_addresses):
    """Display trader portfolio analysis results"""
    st.header("👤 Trader Portfolio Analysis")
    
    if not results:
        st.warning("No trader portfolio data found.")
        return
    
    # === Overview Metrics ===
    col1, col2, col3, col4 = st.columns(4)
    
    total_portfolios = len(results)
    total_value = sum(w.get('total_value_usd', 0) for w in results)
    avg_roi = np.mean([w.get('roi', 0) for w in results if w.get('roi') is not None])
    profitable_wallets = sum(1 for w in results if (w.get('realized_pnl', 0) + w.get('unrealized_pnl', 0)) > 0)
    
    with col1:
        st.metric("Portfolios Analyzed", total_portfolios)
    with col2:
        st.metric("Total Portfolio Value", format_usd(total_value))
    with col3:
        st.metric("Average ROI", format_percentage(avg_roi))
    with col4:
        st.metric("Profitable Wallets", profitable_wallets)
    
    # === Portfolio Details ===
    st.subheader("📈 Portfolio Performance")
    
    for wallet in results:
        wallet_addr = wallet.get('wallet_address', '')
        
        with st.expander(f"Wallet: {truncate_address(wallet_addr)}"):
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Total Value", format_usd(wallet.get('total_value_usd', 0)))
                st.metric("SOL Balance", f"{wallet.get('sol_balance', 0):.4f} SOL")
            
            with col2:
                st.metric("Realized PnL", format_usd(wallet.get('realized_pnl', 0)))
                st.metric("Unrealized PnL", format_usd(wallet.get('unrealized_pnl', 0)))
            
            with col3:
                st.metric("Total PnL", format_usd(wallet.get('total_pnl', 0)))
                st.metric("ROI", format_percentage(wallet.get('roi', 0)))
            
            # Performance breakdown
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Tokens in Profit", wallet.get('num_tokens_in_profit', 0))
            with col2:
                st.metric("Tokens in Loss", wallet.get('num_tokens_in_loss', 0))
            with col3:
                st.metric("Tokens Holding", wallet.get('num_tokens_holding', 0))
            with col4:
                total_buy_vol = wallet.get('total_buy_volume', 0)
                st.metric("Total Invested", format_usd(total_buy_vol))
            
            # Token holdings
            tokens = wallet.get('tokens', [])
            if tokens:
                st.write("**Current Token Holdings:**")
                token_data = []
                for token in tokens:
                    trade_data = token.get('trade_data', {})
                    token_data.append({
                        'Token': truncate_address(token.get('mint', '')),
                        'Amount': f"{token.get('amount', 0):.6f}",
                        'Current Price': format_usd(token.get('price_usd', 0)),
                        'Value': format_usd(token.get('value_usd', 0)),
                        'Avg Buy Price': format_usd(trade_data.get('avg_buy_price', 0)) if trade_data.get('avg_buy_price') else "N/A",
                        'Unrealized PnL': format_usd(token.get('unrealized_pnl', 0)),
                        'Status': trade_data.get('status', 'Unknown')
                    })
                
                if token_data:
                    st.dataframe(pd.DataFrame(token_data), use_container_width=True)

def display_wallet_analysis(results, token_addresses):
    """Display comprehensive wallet analysis"""
    st.header("👛 Wallet Analysis")
    
    if not results:
        st.warning("No wallet data found.")
        return
    
    # === Summary Cards ===
    total_value = sum(w.get('total_value_usd', 0) for w in results)
    total_realized_pnl = sum(w.get('realized_pnl', 0) for w in results)
    total_unrealized_pnl = sum(w.get('unrealized_pnl', 0) for w in results)
    avg_roi = np.mean([w.get('roi', 0) for w in results if w.get('roi') is not None])
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""
        <div class="metric-card">
            <h3>💰 Total Portfolio Value</h3>
            <h2>{format_usd(total_value)}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        pnl_class = "profit-card" if total_realized_pnl > 0 else "loss-card"
        st.markdown(f"""
        <div class="{pnl_class}">
            <h3>📊 Realized PnL</h3>
            <h2>{format_usd(total_realized_pnl)}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        unrealized_class = "profit-card" if total_unrealized_pnl > 0 else "loss-card"
        st.markdown(f"""
        <div class="{unrealized_class}">
            <h3>⏳ Unrealized PnL</h3>
            <h2>{format_usd(total_unrealized_pnl)}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        roi_class = "profit-card" if avg_roi > 0 else "loss-card"
        st.markdown(f"""
        <div class="{roi_class}">
            <h3>📈 Average ROI</h3>
            <h2>{format_percentage(avg_roi)}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    # === Portfolio Visualization ===
    st.subheader("📊 Portfolio Breakdown")
    
    # Create portfolio composition chart
    portfolio_data = []
    for wallet in results:
        wallet_addr = truncate_address(wallet.get('wallet_address', ''))
        sol_value = wallet.get('sol_value_usd', 0)
        tokens_value = wallet.get('tokens_value_usd', 0)
        
        portfolio_data.append({
            'Wallet': wallet_addr,
            'SOL Value': sol_value,
            'Tokens Value': tokens_value,
            'Total Value': sol_value + tokens_value
        })
    
    if portfolio_data:
        portfolio_df = pd.DataFrame(portfolio_data)
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Portfolio value distribution
            fig_portfolio = px.bar(
                portfolio_df,
                x='Wallet',
                y=['SOL Value', 'Tokens Value'],
                title="Portfolio Value Distribution",
                color_discrete_map={
                    'SOL Value': '#9945FF',
                    'Tokens Value': '#14F195'
                }
            )
            st.plotly_chart(fig_portfolio, use_container_width=True)
        
        with col2:
            # Total value pie chart
            if len(results) > 1:
                fig_pie = px.pie(
                    portfolio_df,
                    values='Total Value',
                    names='Wallet',
                    title="Portfolio Value Share"
                )
                st.plotly_chart(fig_pie, use_container_width=True)
    
    # === Detailed Wallet Information ===
    st.subheader("🔍 Detailed Wallet Information")
    
    for i, wallet in enumerate(results):
        wallet_addr = wallet.get('wallet_address', '')
        
        with st.expander(f"Wallet {i+1}: {truncate_address(wallet_addr)}", expanded=i==0):
            # Basic wallet info
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("SOL Balance", f"{wallet.get('sol_balance', 0):.4f}")
            with col2:
                st.metric("SOL Value", format_usd(wallet.get('sol_value_usd', 0)))
            with col3:
                st.metric("Tokens Value", format_usd(wallet.get('tokens_value_usd', 0)))
            with col4:
                st.metric("Total Value", format_usd(wallet.get('total_value_usd', 0)))
            
            # Performance metrics
            st.write("**Performance Metrics:**")
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Realized PnL", format_usd(wallet.get('realized_pnl', 0)))
            with col2:
                st.metric("Unrealized PnL", format_usd(wallet.get('unrealized_pnl', 0)))
            with col3:
                st.metric("ROI", format_percentage(wallet.get('roi', 0)))
            
            # Token holdings table
            tokens = wallet.get('tokens', [])
            if tokens:
                st.write("**Token Holdings:**")
                token_holdings = []
                for token in tokens:
                    token_holdings.append({
                        'Token Mint': truncate_address(token.get('mint', '')),
                        'Amount': f"{token.get('amount', 0):.6f}",
                        'Price (USD)': format_usd(token.get('price_usd', 0)),
                        'Value (USD)': format_usd(token.get('value_usd', 0)),
                        'Avg Buy Price': format_usd(token.get('avg_buy_price', 0)) if token.get('avg_buy_price') else "N/A",
                        'Unrealized PnL': format_usd(token.get('unrealized_pnl', 0))
                    })
                
                st.dataframe(pd.DataFrame(token_holdings), use_container_width=True)
            
            # Trade metrics for this wallet
            trade_metrics = wallet.get('trade_metrics', [])
            if trade_metrics:
                st.write("**Trade History Summary:**")
                for metric in trade_metrics:
                    token_breakdown = metric.get('token_breakdown', [])
                    if token_breakdown:
                        breakdown_data = []
                        for token_trade in token_breakdown:
                            breakdown_data.append({
                                'Token': truncate_address(token_trade.get('token_mint', '')),
                                'Buy Volume': format_usd(token_trade.get('buy_volume_usd', 0)),
                                'Sell Volume': format_usd(token_trade.get('sell_volume_usd', 0)),
                                'PnL': format_usd(token_trade.get('pnl_usd', 0)),
                                'ROI': format_percentage(token_trade.get('roi_percent', 0)),
                                'Status': token_trade.get('status', 'Unknown')
                            })
                        
                        if breakdown_data:
                            st.dataframe(pd.DataFrame(breakdown_data), use_container_width=True)

# === Raw Data Viewer ===
def display_raw_data_viewer():
    """Display raw data for debugging purposes"""
    st.sidebar.subheader("🔍 Raw Data Viewer")
    if st.sidebar.button("Show Raw Trade Data"):
        if 'last_analysis_data' in st.session_state:
            st.subheader("📋 Raw Analysis Data")
            st.json(st.session_state.last_analysis_data)

# === Advanced Analytics Section ===
def display_advanced_analytics():
    """Display advanced analytics and insights"""
    st.sidebar.subheader("📊 Advanced Analytics")
    
    if st.sidebar.button("Generate Insights"):
        st.subheader("🧠 Advanced Insights")
        
        # This could include more sophisticated analysis
        st.info("Advanced analytics features coming soon!")
        st.write("""
        Future features may include:
        - Time-series analysis of trading patterns
        - Correlation analysis between tokens
        - Risk assessment metrics
        - Trading strategy identification
        - Market timing analysis
        """)

# === App Footer ===
def display_footer():
    st.markdown("---")
    st.markdown("""
    <div style='text-align: center; color: #666; padding: 20px;'>
        Smart Trader Analytics Dashboard v1.0<br>
        Built with Streamlit • Powered by Helius & Birdeye APIs
    </div>
    """, unsafe_allow_html=True)

# === Main App Execution ===
if __name__ == "__main__":
    main()
    
    # === Additional Features in Sidebar ===
    st.sidebar.markdown("---")
    display_raw_data_viewer()
    display_advanced_analytics()
    
    # === Footer ===
    display_footer()
    
    # === Session State Management ===
    if 'analysis_complete' not in st.session_state:
        st.session_state.analysis_complete = False
    
    # === Help Section ===
    with st.sidebar.expander("❓ Help & Instructions"):
        st.markdown("""
        **How to use this app:**
        
        1. **Select Analysis Type**: Choose between token analysis, trader portfolio analysis, or wallet analysis
        
        2. **Enter Token Addresses**: Provide 1-3 Solana token mint addresses
        
        3. **Configure Parameters**: Adjust trade limit and other settings
        
        4. **Run Analysis**: Click the "Run Analysis" button to start
        
        **Analysis Types:**
        - **Token Trade Analysis**: Analyze trading patterns for specific tokens
        - **Trader Portfolio Analysis**: Analyze portfolios of traders who trade your tokens
        - **Wallet Analysis**: Deep dive into specific wallet performance
        
        **Note:** Please do not over use this service, it is just a test, the API compute unit could exhaust!!!
        """)