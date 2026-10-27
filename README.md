# Trade Management Dashboard

A real-time trade management dashboard that helps you monitor your stock trades and provides visual sell signals based on ATR (Average True Range) trailing stops.

## Features

- 📊 **Live Data**: Real-time stock prices from Yahoo Finance
- 🎨 **Visual Signals**: Color-coded trade status (Green=Active, Red=Sell Signal, Gray=Closed)
- 📈 **Interactive Charts**: Price charts with entry and stop-loss levels
- 🔄 **Auto-Refresh**: Updates every 30 seconds
- 📱 **Responsive**: Works on desktop and mobile

## How It Works

1. **Add a trade**: Enter ticker symbol, entry price, stop-loss %, and trailing multiplier
2. **Monitor**: The dashboard tracks your trade and calculates the trailing stop based on ATR
3. **Act**: When the price hits the trailing stop, the tile turns RED - time to sell!
4. **Archive**: After selling, mark the trade as closed (turns gray)

## Setup

### Local Development

1. Clone this repository:
```bash
git clone https://github.com/YOUR_USERNAME/trade-dashboard.git
cd trade-dashboard
