# ClawBots Polymarket Bot 🔫🧬

Autonomous Polymarket trading bot with mispricing detection, cross-market arbitrage, and safe position management.

## Features

- **Market Scanner**: Detects mispriced markets by checking YES+NO sums and cross-market correlations
- **Wallet Management**: Create and manage Polygon wallets for trading
- **Safe Trading**: Dry-run mode, position limits, daily loss caps
- **Position Tracking**: Real-time P&L monitoring
- **CLOB Integration**: Full order placement, cancellation, and management via Polymarket's CLOB API

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp config/.env.example config/.env

# Create a new wallet
python -m src.wallet.create_wallet

# Start the bot (dry-run first!)
python -m src.bot --dry-run
```

## Configuration

All settings are in `config/.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `POLY_PRIVATE_KEY` | Your Polygon wallet private key | Required |
| `DRY_RUN` | Simulate trades without executing | `true` |
| `MAX_POSITION_USD` | Maximum USD per single position | `50` |
| `MAX_DAILY_LOSS_USD` | Stop trading after this daily loss | `20` |
| `MIN_EDGE_PCT` | Minimum mispricing edge to trade | `3` |
| `SCAN_INTERVAL_SEC` | Seconds between market scans | `60` |

## Safety

- **Dry-run mode** is ON by default. No real trades happen until you explicitly disable it.
- **Position limits** cap your exposure per market.
- **Daily loss limits** stop the bot if it's losing.
- All trades are logged with full audit trail.

## Architecture

```
src/
├── wallet/       # Wallet creation, key management
├── scanner/      # Market scanning, mispricing detection
├── trader/       # Order placement, CLOB integration
├── positions/    # Position tracking, P&L
├── utils/        # API clients, formatting, logging
└── bot.py        # Main orchestration loop
```

## License

MIT