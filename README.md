# Smart Trader Analytics Dashboard

A comprehensive Solana blockchain analytics tool that analyzes token trading patterns, trader performance, and wallet portfolios using real-time data from Helius and Birdeye APIs.

## Features

- **Token Trade Analysis**: Analyze trading patterns and metrics for specific Solana tokens
- **Trader Portfolio Analysis**: Examine portfolios of traders who trade your selected tokens
- **Wallet Analysis**: Deep dive into specific wallet performance and holdings
- **Real-time Price Data**: Current and historical price data integration
- **PnL Calculations**: Comprehensive profit/loss analysis with realized and unrealized gains
- **Interactive Dashboard**: Web-based Streamlit interface with visualizations
- **Caching System**: Efficient caching to minimize API calls and improve performance

## Project Structure

```
smart-trader-analytics/
├── refactored_smart_trader.py    # Core trading analysis engine
├── streamlit_app.py              # Streamlit web interface
├── requirements.txt              # Python dependencies
├── .env                          # Environment variables (API keys)
├── token_trade_cache/            # Token trade data cache
├── price_cache/                  # Price data cache
├── trade_cache.json             # Trade cache file
└── README.md                    # This file
```

## Installation

### Prerequisites

- Python 3.8 or higher
- Helius API key (for Solana RPC and transaction data)
- Birdeye API key (for price data)

### Setup

1. **Clone or download the project files**
   ```bash
   # Ensure you have both Python files in the same directory
   # refactored_smart_trader.py
   # streamlit_app.py
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Configuration**
   
   Create a `.env` file in the project root:
   ```env
   HELIUS_API_KEY=your_helius_api_key_here
   BIRDEYE_API_KEY=your_birdeye_api_key_here
   ```

4. **Get API Keys**
   - **Helius**: Sign up at [helius.xyz](https://helius.xyz) for Solana RPC and transaction data
   - **Birdeye**: Get API key from [birdeye.so](https://birdeye.so) for price data

## Usage

### Web Interface (Recommended)

1. **Start the Streamlit app**:
   ```bash
   streamlit run streamlit_app.py
   ```

2. **Open your browser** to the URL shown (typically `http://localhost:8501`)

3. **Configure your analysis**:
   - Select analysis type (Token Trade, Trader Portfolio, or Wallet Analysis)
   - Enter token mint addresses (max 3 per analysis)
   - For wallet analysis, provide wallet addresses
   - Adjust trade limit (50-500 transactions)

4. **Run analysis** and view interactive results with charts and metrics

### Command Line Interface

You can also run analysis directly from the command line:

```bash
# Analyze specific tokens
python refactored_smart_trader.py --tokens 6fzQtvZ224efM1Mai7avfLWke59ipLfdWaJ21UdYpump

# Analyze specific wallets
python refactored_smart_trader.py --wallets 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU --tokens 6fzQtvZ224efM1Mai7avfLWke59ipLfdWaJ21UdYpump
```

## Key Features Explained

### Analysis Types

1. **Token Trade Analysis**
   - Fetches all trades for specified tokens
   - Calculates trader performance metrics
   - Shows PnL, ROI, and trading patterns
   - Identifies top performers

2. **Trader Portfolio Analysis**
   - Analyzes current holdings of traders who trade your tokens
   - Shows unrealized PnL based on current prices
   - Portfolio composition and value distribution

3. **Wallet Analysis**
   - Comprehensive wallet analysis including SOL balance
   - Token holdings with current values
   - Historical trading performance
   - Combined realized and unrealized PnL

### Metrics Calculated

- **Realized PnL**: Profit/loss from completed trades (sold positions)
- **Unrealized PnL**: Current profit/loss on held positions
- **ROI**: Return on investment percentage
- **Buy/Sell Volume**: Total USD volume of purchases and sales
- **Average Buy Price**: Volume-weighted average purchase price
- **Position Status**: Holding, Partial, Profit, or Loss

### Caching System

The application implements multiple caching layers:
- **Trade Cache**: Stores transaction data to avoid refetching
- **Price Cache**: Caches current and historical price data
- **In-memory Cache**: Quick access for frequently requested data

## Configuration

### Rate Limiting

The application includes built-in rate limiting to respect API limits:
- **Helius**: 9 requests per second
- **Birdeye**: 1 request per second (conservative to avoid 429 errors)

### Cache Settings

- **Price Cache Expiry**: 5 minutes for current prices
- **Trade Cache**: Persistent across sessions
- **Historical Price Cache**: Daily chunks cached indefinitely

## API Requirements

### Helius API
- Used for Solana RPC calls and transaction data
- Endpoints used:
  - Transaction history
  - Wallet balances
  - Token account information

### Birdeye API
- Used for current and historical price data
- Endpoints used:
  - Current token prices
  - Historical price data (30-minute intervals)

## Performance Considerations

- **Trade Limit**: Lower limits (50-100) provide faster results
- **Token Limit**: Maximum 3 tokens per analysis to manage API usage
- **Caching**: Extensive caching reduces API calls and improves speed
- **Rate Limiting**: Conservative rate limits prevent API quota exhaustion

## Limitations

- Maximum 3 token addresses per analysis
- Historical price data limited to available Birdeye data
- Rate limits may cause slower processing for large datasets
- Requires active internet connection for API calls

## Troubleshooting

### Common Issues

1. **Import Error**: Ensure both Python files are in the same directory
2. **API Key Errors**: Verify your `.env` file has valid API keys
3. **Invalid Addresses**: Use only valid Solana token mint addresses
4. **Rate Limit Errors**: Reduce trade limit or wait between analyses
5. **No Data Found**: Verify token addresses are actively traded

### Logging

The application provides detailed logging for debugging:
- API request/response information
- Cache hit/miss statistics
- Error messages with context

## Development

### Key Components

- **HttpClient**: Async HTTP client with rate limiting and retry logic
- **Trade Analysis**: Functions for fetching and processing trade data
- **Price Management**: Current and historical price fetching with caching
- **Metrics Calculation**: PnL, ROI, and performance calculations
- **Streamlit Interface**: Interactive web dashboard

### Extending the Application

The modular design allows easy extension:
- Add new analysis types in the Streamlit interface
- Implement additional metrics in the core trading module
- Integrate new data sources or APIs
- Add more visualization types

## Security Notes

- Store API keys securely in `.env` file
- Never commit API keys to version control
- Be mindful of API rate limits and costs
- Cache files may contain sensitive trading data

## Support

For issues or questions:
1. Check the troubleshooting section
2. Review logs for error details
3. Verify API key validity and permissions
4. Ensure all dependencies are correctly installed

## License

This project is for educational and research purposes. Ensure compliance with all applicable API terms of service and trading regulations in your jurisdiction.

---

**Note**: This tool is for analysis purposes only and should not be considered financial advice. Always do your own research before making trading decisions.