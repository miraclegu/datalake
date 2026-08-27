#!/usr/bin/env python3
"""
技术指标计算模块 - 适用于 tdx2db 数据库
"""
import pandas as pd
import numpy as np
import duckdb
from typing import Optional, Dict, List, Tuple
from pathlib import Path

# ==================== 指标计算核心函数 ====================

def calculate_ma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """计算移动平均线 (Moving Average)"""
    df = df.copy()
    df[f'MA{period}'] = df['close'].rolling(window=period).mean()
    return df

def calculate_ema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """计算指数移动平均线 (Exponential Moving Average)"""
    df = df.copy()
    df[f'EMA{period}'] = df['close'].ewm(span=period, adjust=False).mean()
    return df

def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """计算MACD (Moving Average Convergence Divergence)"""
    df = df.copy()
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['MACD'] = ema_fast - ema_slow
    df['MACD_Signal'] = df['MACD'].ewm(span=signal, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
    return df

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算相对强弱指标 (Relative Strength Index)"""
    df = df.copy()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    df[f'RSI{period}'] = 100 - (100 / (1 + rs))
    return df

def calculate_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """计算KDJ指标"""
    df = df.copy()
    low_list = df['low'].rolling(n, min_periods=1).min()
    high_list = df['high'].rolling(n, min_periods=1).max()
    rsv = (df['close'] - low_list) / (high_list - low_list) * 100
    df['KDJ_K'] = rsv.ewm(com=m1-1, adjust=False).mean()
    df['KDJ_D'] = df['KDJ_K'].ewm(com=m2-1, adjust=False).mean()
    df['KDJ_J'] = 3 * df['KDJ_K'] - 2 * df['KDJ_D']
    return df

def calculate_boll(df: pd.DataFrame, period: int = 20, std_dev: int = 2) -> pd.DataFrame:
    """计算布林带 (Bollinger Bands)"""
    df = df.copy()
    df['BOLL_Mid'] = df['close'].rolling(period).mean()
    std = df['close'].rolling(period).std()
    df['BOLL_Upper'] = df['BOLL_Mid'] + (std * std_dev)
    df['BOLL_Lower'] = df['BOLL_Mid'] - (std * std_dev)
    return df

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算真实波幅 (Average True Range)"""
    df = df.copy()
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = true_range.rolling(window=period).mean()
    return df

def calculate_obv(df: pd.DataFrame) -> pd.DataFrame:
    """计算能量潮 (On Balance Volume)"""
    df = df.copy()
    obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    df['OBV'] = obv
    return df

def calculate_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """计算成交量加权平均价 (Volume Weighted Average Price)"""
    df = df.copy()
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    df['VWAP'] = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    return df

def calculate_cci(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """计算顺势指标 (Commodity Channel Index)"""
    df = df.copy()
    tp = (df['high'] + df['low'] + df['close']) / 3
    ma_tp = tp.rolling(window=period).mean()
    md = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean())
    df[f'CCI{period}'] = (tp - ma_tp) / (0.015 * md)
    return df

def calculate_roc(df: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    """计算变动率指标 (Rate of Change)"""
    df = df.copy()
    df[f'ROC{period}'] = ((df['close'] - df['close'].shift(period)) / df['close'].shift(period)) * 100
    return df

def calculate_williams_r(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算威廉指标 (Williams %R)"""
    df = df.copy()
    highest_high = df['high'].rolling(window=period).max()
    lowest_low = df['low'].rolling(window=period).min()
    df['WR'] = ((highest_high - df['close']) / (highest_high - lowest_low)) * -100
    return df

def calculate_dmi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算趋向指标 (Directional Movement Index)"""
    df = df.copy()
    
    plus_dm = np.where((df['high'] - df['high'].shift()) > (df['low'].shift() - df['low']), 
                       np.maximum(df['high'] - df['high'].shift(), 0), 0)
    minus_dm = np.where((df['low'].shift() - df['low']) > (df['high'] - df['high'].shift()), 
                        np.maximum(df['low'].shift() - df['low'], 0), 0)
    
    tr1 = df['high'] - df['low']
    tr2 = np.abs(df['high'] - df['close'].shift())
    tr3 = np.abs(df['low'] - df['close'].shift())
    tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    
    atr = tr.rolling(window=period).mean()
    
    plus_di = (pd.Series(plus_dm, index=df.index).rolling(window=period).mean() / atr) * 100
    minus_di = (pd.Series(minus_dm, index=df.index).rolling(window=period).mean() / atr) * 100
    
    df['PDI'] = plus_di
    df['MDI'] = minus_di
    df['ADX'] = (np.abs(plus_di - minus_di) / (plus_di + minus_di) * 100).rolling(window=period).mean()
    
    return df

def calculate_bias(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """计算乖离率 (BIAS)"""
    df = df.copy()
    ma = df['close'].rolling(window=period).mean()
    df[f'BIAS{period}'] = (df['close'] - ma) / ma * 100
    return df

def calculate_volatility(df: pd.DataFrame) -> pd.DataFrame:
    """计算历史波动率"""
    df = df.copy()
    df['DAILY_RETURN'] = df['close'].pct_change()
    df['VOLATILITY_20'] = df['DAILY_RETURN'].rolling(window=20).std() * np.sqrt(252) * 100
    df['VOLATILITY_60'] = df['DAILY_RETURN'].rolling(window=60).std() * np.sqrt(252) * 100
    return df

def calculate_returns_drawdown(df: pd.DataFrame) -> pd.DataFrame:
    """计算收益率和回撤"""
    df = df.copy()
    df['RETURN_5'] = (df['close'] - df['close'].shift(5)) / df['close'].shift(5) * 100
    df['RETURN_10'] = (df['close'] - df['close'].shift(10)) / df['close'].shift(10) * 100
    df['RETURN_20'] = (df['close'] - df['close'].shift(20)) / df['close'].shift(20) * 100
    df['RETURN_60'] = (df['close'] - df['close'].shift(60)) / df['close'].shift(60) * 100
    
    rolling_max_20 = df['close'].rolling(window=20, min_periods=1).max()
    df['DRAWDOWN_20'] = (df['close'] - rolling_max_20) / rolling_max_20 * 100
    
    rolling_max_60 = df['close'].rolling(window=60, min_periods=1).max()
    df['DRAWDOWN_60'] = (df['close'] - rolling_max_60) / rolling_max_60 * 100
    return df

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标
    """
    df = df.copy()
    
    # 确保数据按日期排序
    if 'date' in df.columns:
        df = df.sort_values('date')
    
    # 移动平均线 - 核心均线
    for period in [5, 10, 20, 60, 200, 250, 500]:
        df = calculate_ma(df, period)
    
    # 指数移动平均线
    for period in [5, 10, 12, 20, 26]:
        df = calculate_ema(df, period)
    
    # MACD
    df = calculate_macd(df)
    
    # RSI
    df = calculate_rsi(df, 6)
    df = calculate_rsi(df, 14)
    df = calculate_rsi(df, 24)
    
    # KDJ
    df = calculate_kdj(df)
    
    # 布林带
    df = calculate_boll(df)
    
    # ATR
    df = calculate_atr(df)
    
    # OBV
    df = calculate_obv(df)
    
    # VWAP
    df = calculate_vwap(df)
    
    # CCI
    df = calculate_cci(df, 10)
    df = calculate_cci(df, 20)
    
    # ROC
    df = calculate_roc(df, 12)
    
    # William %R
    df = calculate_williams_r(df)
    
    # DMI
    df = calculate_dmi(df)
    
    # BIAS
    df = calculate_bias(df, 6)
    df = calculate_bias(df, 20)
    df = calculate_bias(df, 60)
    
    # 历史波动率
    df = calculate_volatility(df)
    
    # 收益率和回撤
    df = calculate_returns_drawdown(df)
    
    # 前20日最高价（不含当日）
    df['high_20d'] = df['high'].rolling(window=20, min_periods=1).max().shift(1)
    
    # 成交量均线
    df['volume_ma5'] = df['volume'].rolling(window=5).mean()
    df['volume_ma10'] = df['volume'].rolling(window=10).mean()
    df['volume_ma20'] = df['volume'].rolling(window=20).mean()
    
    # 棘轮式止损价 = high_20d - 3 * ATR
    if 'ATR' in df.columns:
        df['trailing_stop'] = df['high_20d'] - 3 * df['ATR']
    else:
        df['trailing_stop'] = np.nan
    
    # 统一日期为 'YYYY-MM-DD' 字符串（兼容各种输入格式）
    if 'date' in df.columns:
        df['date'] = df['date'].astype(str).str[:10]
    
    return df


# ==================== 数据库操作函数已于 2026-08-23 移出 ====================
# 原先此处有 get_stock_data / save_indicators_to_db / get_last_indicator_date /
# batch_process_stocks* 等 12 个函数, 全部围绕 `stock_indicators` 表读写。
# 该表已删除(见 docs/reports/2026-08-23-conclusion.md), 这些函数**永远不可能成功**,
# 而其中 get_last_indicator_date 是 `except: return None` —— 会静默返回"没有指标数据",
# 调用方无法区分"表没了"和"表是空的"。与其留一个注定腐坏的接口, 直接移出:
#   archive/2026-08-23-l1-research/tdx2db/scripts/indicators_db_side.py
#
# 本模块现在只做一件事: **纯 DataFrame 指标计算**。唯一的活跃消费方是实时监控
# (monitor/tdx2db_adapter.py 按文件路径 import calculate_all_indicators)。
# 取数请用 v_stock_hfq / v_etf_hfq 视图。
# ===========================================================================

def get_indicator_list() -> Dict:
    """获取支持的指标列表"""
    return {
        'MA': ['MA5', 'MA10', 'MA20', 'MA60', 'MA200', 'MA250', 'MA500'],
        'EMA': ['EMA5', 'EMA10', 'EMA12', 'EMA20', 'EMA26'],
        'MACD': ['MACD', 'MACD_Signal', 'MACD_Hist'],
        'RSI': ['RSI6', 'RSI14', 'RSI24'],
        'KDJ': ['KDJ_K', 'KDJ_D', 'KDJ_J'],
        'BOLL': ['BOLL_Upper', 'BOLL_Mid', 'BOLL_Lower'],
        'ATR': ['ATR'],
        'OBV': ['OBV'],
        'VWAP': ['VWAP'],
        'CCI': ['CCI10', 'CCI20'],
        'ROC': ['ROC12'],
        'WR': ['WR'],
        'DMI': ['PDI', 'MDI', 'ADX'],
        'BIAS': ['BIAS6', 'BIAS20', 'BIAS60'],
        'VOLATILITY': ['VOLATILITY_20', 'VOLATILITY_60', 'DAILY_RETURN'],
        'RETURNS': ['RETURN_5', 'RETURN_10', 'RETURN_20', 'RETURN_60',
                    'DRAWDOWN_20', 'DRAWDOWN_60'],
        'PRICE_VOLUME': ['high_20d', 'trailing_stop',
                         'volume_ma5', 'volume_ma10', 'volume_ma20']
    }

if __name__ == "__main__":
    print("这是一个模块文件，请通过其他脚本导入使用")
