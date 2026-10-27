import yfinance as yf
import pandas as pd
import numpy as np

def calculate_atr(ticker, period=14):
    """Calculate Average True Range for volatility measurement"""
    stock = yf.Ticker(ticker)
    hist = stock.history(period="1mo")
    
    if len(hist) < period:
        return None
    
    high = hist['High']
    low = hist['Low']
    close = hist['Close']
    
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    
    return atr

def get_current_price(ticker):
    """Get current price from yfinance"""
    stock = yf.Ticker(ticker)
    data = stock.history(period="1d")
    
    if len(data) == 0:
        return None
    
    return data['Close'].iloc[-1]

def get_price_history(ticker, period="1mo"):
    """Get historical price data for charts"""
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period)
    return hist
