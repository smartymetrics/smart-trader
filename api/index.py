# api/index.py - Main Vercel serverless function
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd

app = Flask(__name__)
CORS(app)

# Environment variables
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BIRDEYE_URL = "https://public-api.birdeye.so"
HISTORY_PRICE_ENDPOINT = f"{BIRDEYE_URL}/defi/history_price"

def get_token_trades_by_address(address: str, limit: int = 100, transaction_type: str = "SWAP"):
    """
    Fetch swap transactions for any address (token mint, wallet, etc.) via Helius Enhanced Transactions API.
    """
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit,
        "type": transaction_type,
        "commitment": "finalized"
    }
    
    try:
        resp = requests.get(url, params=params, timeout=30)
        
        if resp.status_code == 404:
            print(f"⚠️  Address {address} not found or has no transactions (404).")
            return []
        elif resp.status_code == 429:
            print("⚠️  Rate limited. Please wait and try again.")
            return []
        
        resp.raise_for_status()
        return resp.json() or []
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        return []

def get_historical_price(mint_address: str, time_from: int, time_to: int) -> dict:
    """
    Fetches historical price data for a given mint address and time range.
    """
    if not BIRDEYE_API_KEY:
        print("Error: BIRDEYE_API_KEY environment variable is not set.")
        return {}

    headers = {
        "X-API-KEY": BIRDEYE_API_KEY,
        "Accept": "application/json"
    }

    params = {
        "address": mint_address,
        "type": "1H",
        "time_from": time_from,
        "time_to": time_to
    }

    try:
        print(f"Fetching historical price for {mint_address}...")
        response = requests.get(HISTORY_PRICE_ENDPOINT, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
        return {}
    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred: {req_err}")
        return {}
    except json.JSONDecodeError:
        print("Failed to parse JSON response.")
        return {}

def parse_trade_data(transactions):
    """
    Parse token transfers and calculate trader positions
    """
    trader_positions = {}
    
    for tx in transactions:
        if not tx.get('tokenTransfers') or len(tx['tokenTransfers']) == 0:
            continue
        
        for transfer in tx['tokenTransfers']:
            from_token_account = transfer.get('fromTokenAccount', '')
            to_token_account = transfer.get('toTokenAccount', '')
            from_user_account = transfer.get('fromUserAccount', '')
            to_user_account = transfer.get('toUserAccount', '')
            token_amount = float(transfer.get('tokenAmount', 0))
            mint = transfer.get('mint', '')
            token_standard = transfer.get('tokenStandard', '')
            
            # Skip if not fungible token or no amount
            if token_standard != 'Fungible' or token_amount == 0:
                continue
            
            # Initialize trader positions
            for user_account in [from_user_account, to_user_account]:
                if user_account and user_account not in trader_positions:
                    trader_positions[user_account] = {
                        'walletAddress': user_account,
                        'totalTokensBought': 0,
                        'totalTokensSold': 0,
                        'trades': 0,
                        'positions': {}
                    }
            
            # Track positions
            if from_user_account:
                trader = trader_positions[from_user_account]
                trader['totalTokensSold'] += token_amount
                trader['trades'] += 1
                
                if mint not in trader['positions']:
                    trader['positions'][mint] = {'bought': 0, 'sold': 0}
                trader['positions'][mint]['sold'] += token_amount
            
            if to_user_account:
                trader = trader_positions[to_user_account]
                trader['totalTokensBought'] += token_amount
                trader['trades'] += 1
                
                if mint not in trader['positions']:
                    trader['positions'][mint] = {'bought': 0, 'sold': 0}
                trader['positions'][mint]['bought'] += token_amount
    
    return trader_positions

def calculate_roi(trader_positions, price_data, target_mint):
    """
    Calculate ROI using price data
    """
    traders = []
    
    if not price_data.get('data', {}).get('items'):
        return traders
    
    # Sort price data by timestamp
    price_items = price_data['data']['items']
    if not price_items:
        return traders
    
    sorted_prices = sorted(price_items, key=lambda x: x.get('unixTime', 0))
    earliest_price = sorted_prices[0].get('value', 0) if sorted_prices else 0
    latest_price = sorted_prices[-1].get('value', 0) if sorted_prices else 0
    
    # Calculate average price
    avg_price = sum(p.get('value', 0) for p in sorted_prices) / len(sorted_prices) if sorted_prices else 0
    
    for trader_address, trader in trader_positions.items():
        position = trader['positions'].get(target_mint)
        if not position:
            continue
        
        net_tokens = position['bought'] - position['sold']
        if net_tokens == 0:
            continue
        
        # Calculate cost basis and current value
        cost_basis = position['bought'] * avg_price
        current_value = net_tokens * latest_price
        
        if cost_basis > 0:
            roi = ((current_value - cost_basis) / cost_basis) * 100
            
            traders.append({
                'id': trader_address,
                'walletAddress': f"{trader_address[:8]}...{trader_address[-6:]}",
                'fullAddress': trader_address,
                'totalInvested': cost_basis,
                'totalCurrentValue': current_value,
                'totalROI': roi,
                'totalTrades': trader['trades'],
                'tokensBought': position['bought'],
                'tokensSold': position['sold'],
                'netPosition': net_tokens,
                'winRate': 100 if roi > 0 else 0
            })
    
    return traders

@app.route('/api/traders', methods=['POST'])
def get_traders():
    """
    API endpoint to get trader ROI data
    """
    try:
        data = request.get_json()
        token_addresses = data.get('tokenAddresses', [])
        time_range = data.get('timeRange', 7)  # days
        
        if not token_addresses:
            return jsonify({'error': 'No token addresses provided'}), 400
        
        # Calculate time range
        end_time = int(time.time())
        start_time = int(end_time - (time_range * 24 * 60 * 60))
        
        all_traders = []
        
        for token_address in token_addresses:
            # Get trade data
            transactions = get_token_trades_by_address(token_address, limit=200)
            
            if not transactions:
                continue
            
            # Get price data
            price_data = get_historical_price(token_address, start_time, end_time)
            
            if not price_data:
                continue
            
            # Parse and calculate
            trader_positions = parse_trade_data(transactions)
            traders = calculate_roi(trader_positions, price_data, token_address)
            
            all_traders.extend(traders)
        
        # Aggregate traders across multiple tokens
        aggregated_traders = {}
        for trader in all_traders:
            addr = trader['fullAddress']
            if addr in aggregated_traders:
                existing = aggregated_traders[addr]
                existing['totalInvested'] += trader['totalInvested']
                existing['totalCurrentValue'] += trader['totalCurrentValue']
                existing['totalTrades'] += trader['totalTrades']
                if existing['totalInvested'] > 0:
                    existing['totalROI'] = ((existing['totalCurrentValue'] - existing['totalInvested']) / existing['totalInvested']) * 100
            else:
                aggregated_traders[addr] = trader.copy()
        
        result_traders = list(aggregated_traders.values())
        
        # Calculate summary stats
        total_traders = len(result_traders)
        avg_roi = sum(t['totalROI'] for t in result_traders) / total_traders if total_traders > 0 else 0
        total_volume = sum(t['totalInvested'] for t in result_traders)
        
        return jsonify({
            'traders': result_traders,
            'stats': {
                'totalTraders': total_traders,
                'avgROI': avg_roi,
                'totalVolume': total_volume
            }
        })
        
    except Exception as e:
        print(f"Error in get_traders: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """
    Health check endpoint
    """
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'helius_api_configured': bool(HELIUS_API_KEY),
        'birdeye_api_configured': bool(BIRDEYE_API_KEY)
    })

@app.route('/')
def index():
    """
    Serve the main dashboard HTML
    """
    return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Solana Trader ROI Dashboard</title>
    <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
</head>
<body>
    <div id="root"></div>
    
    <script type="text/babel">
        const { useState, useEffect, useMemo } = React;
        const { TrendingUp, TrendingDown, DollarSign, Users, BarChart3, RefreshCw, Plus, X, AlertCircle } = lucide;

        const SolanaTraderDashboard = () => {
            const [tokenAddresses, setTokenAddresses] = useState([
                'FCZm2ngARmPYnX38f6wwcwMCAnX28qqCPgNp6r24fL9k'
            ]);
            const [newTokenAddress, setNewTokenAddress] = useState('');
            const [timeRange, setTimeRange] = useState(7);
            const [loading, setLoading] = useState(false);
            const [tradersData, setTradersData] = useState([]);
            const [stats, setStats] = useState({ totalTraders: 0, avgROI: 0, totalVolume: 0 });
            const [sortBy, setSortBy] = useState('roi');
            const [sortDirection, setSortDirection] = useState('desc');
            const [error, setError] = useState('');

            const fetchTraderData = async () => {
                setLoading(true);
                setError('');
                
                try {
                    const response = await fetch('/api/traders', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            tokenAddresses: tokenAddresses,
                            timeRange: timeRange
                        })
                    });
                    
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    
                    const data = await response.json();
                    setTradersData(data.traders || []);
                    setStats(data.stats || { totalTraders: 0, avgROI: 0, totalVolume: 0 });
                    
                } catch (err) {
                    setError(`Error fetching data: ${err.message}`);
                    console.error('Error fetching trader data:', err);
                } finally {
                    setLoading(false);
                }
            };

            const addToken = () => {
                if (newTokenAddress.trim() && !tokenAddresses.includes(newTokenAddress.trim())) {
                    setTokenAddresses([...tokenAddresses, newTokenAddress.trim()]);
                    setNewTokenAddress('');
                }
            };

            const removeToken = (tokenToRemove) => {
                if (tokenAddresses.length > 1) {
                    setTokenAddresses(tokenAddresses.filter(token => token !== tokenToRemove));
                }
            };

            useEffect(() => {
                fetchTraderData();
            }, [tokenAddresses, timeRange]);

            const sortedTraders = useMemo(() => {
                const sorted = [...tradersData].sort((a, b) => {
                    let aValue, bValue;
                    
                    switch (sortBy) {
                        case 'roi':
                            aValue = a.totalROI;
                            bValue = b.totalROI;
                            break;
                        case 'invested':
                            aValue = a.totalInvested;
                            bValue = b.totalInvested;
                            break;
                        case 'currentValue':
                            aValue = a.totalCurrentValue;
                            bValue = b.totalCurrentValue;
                            break;
                        case 'trades':
                            aValue = a.totalTrades;
                            bValue = b.totalTrades;
                            break;
                        case 'netPosition':
                            aValue = a.netPosition;
                            bValue = b.netPosition;
                            break;
                        default:
                            aValue = a.totalROI;
                            bValue = b.totalROI;
                    }
                    
                    return sortDirection === 'desc' ? bValue - aValue : aValue - bValue;
                });
                
                return sorted;
            }, [tradersData, sortBy, sortDirection]);

            const formatCurrency = (value) => {
                return new Intl.NumberFormat('en-US', {
                    style: 'currency',
                    currency: 'USD',
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 6
                }).format(value);
            };

            const formatNumber = (value) => {
                return new Intl.NumberFormat('en-US', {
                    minimumFractionDigits: 0,
                    maximumFractionDigits: 2
                }).format(value);
            };

            const formatPercentage = (value) => {
                const color = value >= 0 ? 'text-green-600' : 'text-red-600';
                const IconComponent = value >= 0 ? TrendingUp : TrendingDown;
                return React.createElement('span', {
                    className: `flex items-center gap-1 ${color} font-semibold`
                }, [
                    React.createElement(IconComponent, { key: 'icon', className: 'w-4 h-4' }),
                    `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
                ]);
            };

            return React.createElement('div', { className: 'min-h-screen bg-gray-50 p-6' }, 
                React.createElement('div', { className: 'max-w-7xl mx-auto' }, [
                    // Header
                    React.createElement('div', { key: 'header', className: 'mb-8' }, [
                        React.createElement('h1', { key: 'title', className: 'text-3xl font-bold text-gray-900 mb-2' }, 'Solana Trader ROI Dashboard'),
                        React.createElement('p', { key: 'subtitle', className: 'text-gray-600' }, 'Track and rank the most profitable Solana traders across your selected tokens')
                    ]),

                    // Error Message
                    error && React.createElement('div', { key: 'error', className: 'bg-red-50 border border-red-200 rounded-lg p-4 mb-6' },
                        React.createElement('div', { className: 'flex items-center gap-2 text-red-800' }, [
                            React.createElement(AlertCircle, { key: 'icon', className: 'w-5 h-5' }),
                            React.createElement('span', { key: 'label', className: 'font-medium' }, 'Error:'),
                            React.createElement('span', { key: 'message' }, error)
                        ])
                    ),

                    // Token Management
                    React.createElement('div', { key: 'token-management', className: 'bg-white rounded-lg p-6 mb-6 shadow-sm border' }, [
                        React.createElement('h2', { key: 'title', className: 'text-lg font-semibold mb-4' }, 'Token Selection'),
                        React.createElement('div', { key: 'add-token', className: 'flex gap-2 mb-4' }, [
                            React.createElement('input', {
                                key: 'input',
                                type: 'text',
                                value: newTokenAddress,
                                onChange: (e) => setNewTokenAddress(e.target.value),
                                placeholder: 'Enter Solana token mint address',
                                className: 'flex-1 px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500',
                                onKeyPress: (e) => e.key === 'Enter' && addToken()
                            }),
                            React.createElement('button', {
                                key: 'button',
                                onClick: addToken,
                                className: 'px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors flex items-center gap-2'
                            }, [
                                React.createElement(Plus, { key: 'icon', className: 'w-4 h-4' }),
                                'Add Token'
                            ])
                        ]),
                        React.createElement('div', { key: 'token-list', className: 'flex flex-wrap gap-2' },
                            tokenAddresses.map((token, index) => 
                                React.createElement('div', { key: token, className: 'bg-gray-100 rounded-full px-3 py-1 flex items-center gap-2' }, [
                                    React.createElement('span', { key: 'address', className: 'text-sm font-mono' }, 
                                        `${token.substring(0, 8)}...${token.substring(token.length - 6)}`),
                                    tokenAddresses.length > 1 && React.createElement('button', {
                                        key: 'remove',
                                        onClick: () => removeToken(token),
                                        className: 'text-gray-500 hover:text-red-500 transition-colors'
                                    }, React.createElement(X, { className: 'w-3 h-3' }))
                                ])
                            )
                        )
                    ]),

                    // Controls
                    React.createElement('div', { key: 'controls', className: 'bg-white rounded-lg p-6 mb-6 shadow-sm border' },
                        React.createElement('div', { className: 'flex flex-wrap gap-4 items-center justify-between' }, [
                            React.createElement('div', { key: 'controls-left', className: 'flex gap-4 items-center' }, [
                                React.createElement('div', { key: 'time-range' }, [
                                    React.createElement('label', { className: 'block text-sm font-medium text-gray-700 mb-1' }, 'Time Range'),
                                    React.createElement('select', {
                                        value: timeRange,
                                        onChange: (e) => setTimeRange(Number(e.target.value)),
                                        className: 'px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
                                    }, [
                                        React.createElement('option', { key: '1', value: 1 }, 'Last 24 Hours'),
                                        React.createElement('option', { key: '7', value: 7 }, 'Last 7 Days'),
                                        React.createElement('option', { key: '30', value: 30 }, 'Last 30 Days'),
                                        React.createElement('option', { key: '90', value: 90 }, 'Last 90 Days')
                                    ])
                                ]),
                                React.createElement('div', { key: 'sort-by' }, [
                                    React.createElement('label', { className: 'block text-sm font-medium text-gray-700 mb-1' }, 'Sort By'),
                                    React.createElement('select', {
                                        value: sortBy,
                                        onChange: (e) => setSortBy(e.target.value),
                                        className: 'px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500'
                                    }, [
                                        React.createElement('option', { key: 'roi', value: 'roi' }, 'ROI %'),
                                        React.createElement('option', { key: 'invested', value: 'invested' }, 'Total Invested'),
                                        React.createElement('option', { key: 'currentValue', value: 'currentValue' }, 'Current Value'),
                                        React.createElement('option', { key: 'trades', value: 'trades' }, 'Total Trades'),
                                        React.createElement('option', { key: 'netPosition', value: 'netPosition' }, 'Net Position')
                                    ])
                                ])
                            ]),
                            React.createElement('button', {
                                key: 'refresh',
                                onClick: fetchTraderData,
                                disabled: loading,
                                className: 'px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 transition-colors flex items-center gap-2 disabled:opacity-50'
                            }, [
                                React.createElement(RefreshCw, { key: 'icon', className: `w-4 h-4 ${loading ? 'animate-spin' : ''}` }),
                                loading ? 'Loading...' : 'Refresh Data'
                            ])
                        ])
                    ),

                    // Stats Cards
                    React.createElement('div', { key: 'stats', className: 'grid grid-cols-1 md:grid-cols-3 gap-6 mb-6' }, [
                        React.createElement('div', { key: 'avg-roi', className: 'bg-white rounded-lg p-6 shadow-sm border' },
                            React.createElement('div', { className: 'flex items-center justify-between' }, [
                                React.createElement('div', { key: 'content' }, [
                                    React.createElement('p', { key: 'label', className: 'text-sm font-medium text-gray-600' }, 'Average ROI'),
                                    React.createElement('p', { key: 'value', className: 'text-2xl font-bold' }, formatPercentage(stats.avgROI))
                                ]),
                                React.createElement('div', { key: 'icon', className: 'p-3 bg-blue-50 rounded-full' },
                                    React.createElement(BarChart3, { className: 'w-6 h-6 text-blue-600' })
                                )
                            ])
                        ),
                        React.createElement('div', { key: 'total-traders', className: 'bg-white rounded-lg p-6 shadow-sm border' },
                            React.createElement('div', { className: 'flex items-center justify-between' }, [
                                React.createElement('div', { key: 'content' }, [
                                    React.createElement('p', { key: 'label', className: 'text-sm font-medium text-gray-600' }, 'Active Traders'),
                                    React.createElement('p', { key: 'value', className: 'text-2xl font-bold text-gray-900' }, stats.totalTraders)
                                ]),
                                React.createElement('div', { key: 'icon', className: 'p-3 bg-green-50 rounded-full' },
                                    React.createElement(Users, { className: 'w-6 h-6 text-green-600' })
                                )
                            ])
                        ),
                        React.createElement('div', { key: 'total-volume', className: 'bg-white rounded-lg p-6 shadow-sm border' },
                            React.createElement('div', { className: 'flex items-center justify-between' }, [
                                React.createElement('div', { key: 'content' }, [
                                    React.createElement('p', { key: 'label', className: 'text-sm font-medium text-gray-600' }, 'Total Volume'),
                                    React.createElement('p', { key: 'value', className: 'text-2xl font-bold text-gray-900' }, formatCurrency(stats.totalVolume))
                                ]),
                                React.createElement('div', { key: 'icon', className: 'p-3 bg-purple-50 rounded-full' },
                                    React.createElement(DollarSign, { className: 'w-6 h-6 text-purple-600' })
                                )
                            ])
                        )
                    ]),

                    // Traders Table
                    React.createElement('div', { key: 'table', className: 'bg-white rounded-lg shadow-sm border overflow-hidden' }, [
                        React.createElement('div', { key: 'header', className: 'px-6 py-4 border-b border-gray-200' },
                            React.createElement('h2', { className: 'text-lg font-semibold' }, 'Top Traders by ROI')
                        ),
                        loading ? 
                            React.createElement('div', { key: 'loading', className: 'flex items-center justify-center py-12' }, [
                                React.createElement(RefreshCw, { key: 'icon', className: 'w-8 h-8 animate-spin text-gray-400' }),
                                React.createElement('span', { key: 'text', className: 'ml-2 text-gray-600' }, 'Analyzing trader positions...')
                            ]) :
                            React.createElement('div', { key: 'table-content', className: 'overflow-x-auto' },
                                React.createElement('table', { className: 'w-full' }, [
                                    React.createElement('thead', { key: 'thead', className: 'bg-gray-50' },
                                        React.createElement('tr', {}, [
                                            React.createElement('th', { key: 'rank', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Rank'),
                                            React.createElement('th', { key: 'trader', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Trader'),
                                            React.createElement('th', { key: 'roi', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'ROI'),
                                            React.createElement('th', { key: 'invested', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Invested'),
                                            React.createElement('th', { key: 'current', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Current Value'),
                                            React.createElement('th', { key: 'position', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Net Position'),
                                            React.createElement('th', { key: 'trades', className: 'px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider' }, 'Trades')
                                        ])
                                    ),
                                    React.createElement('tbody', { key: 'tbody', className: 'bg-white divide-y divide-gray-200' },
                                        sortedTraders.length > 0 ? 
                                            sortedTraders.slice(0, 50).map((trader, index) =>
                                                React.createElement('tr', { key: trader.id, className: 'hover:bg-gray-50' }, [
                                                    React.createElement('td', { key: 'rank', className: 'px-6 py-4 whitespace-nowrap text-sm text-gray-900' }, `#${index + 1}`),
                                                    React.createElement('td', { key: 'wallet', className: 'px-6 py-4 whitespace-nowrap' },
                                                        React.createElement('div', { className: 'text-sm font-medium text-gray-900 font-mono' }, trader.walletAddress)
                                                    ),
                                                    React.createElement('td', { key: 'roi', className: 'px-6 py-4 whitespace-nowrap' }, formatPercentage(trader.totalROI)),
                                                    React.createElement('td', { key: 'invested', className: 'px-6 py-4 whitespace-nowrap text-sm text-gray-900' }, formatCurrency(trader.totalInvested)),
                                                    React.createElement('td', { key: 'current', className: 'px-6 py-4 whitespace-nowrap text-sm text-gray-900' }, formatCurrency(trader.totalCurrentValue)),
                                                    React.createElement('td', { key: 'position', className: 'px-6 py-4 whitespace-nowrap text-sm text-gray-900' }, formatNumber(trader.netPosition)),
                                                    React.createElement('td', { key: 'trades', className: 'px-6 py-4 whitespace-nowrap text-sm text-gray-900' }, trader.totalTrades)
                                                ])
                                            ) :
                                            React.createElement('tr', { key: 'no-data' },
                                                React.createElement('td', { colSpan: 7, className: 'px-6 py-8 text-center text-gray-500' }, 'No trader data available. Add tokens and refresh to load data.')
                                            )
                                    )
                                ])
                            )
                    ])
                ])
            );
        };

        ReactDOM.render(React.createElement(SolanaTraderDashboard), document.getElementById('root'));
    </script>
</body>
</html>
    '''

# For Vercel deployment
def handler(request):
    return app(request.environ, request.start_response)

if __name__ == '__main__':
    app.run(debug=True)