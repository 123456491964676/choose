"""
A股趋势策略历史回测脚本：正式数据版本 + 指定日期股票池未来走势分析

核心逻辑：
1. 使用完整历史数据；
2. T 日收盘后生成选股信号；
3. T+1 日开盘买入；
4. 持有 HOLD_DAYS 个交易日；
5. 第 HOLD_DAYS 个交易日收盘卖出；
6. 统计单笔收益、相对沪深300超额收益、按信号日等权收益。

新增功能：
1. 指定某个信号日期；
2. 对该日期所有股票进行分类；
3. 找出该日的 ["强趋势池", "趋势观察池"]；
4. 统计这些股票未来 1~N 个交易日的累计收益路径；
5. 第一行放沪深300，方便对比；
6. 输出 Excel 文件。

依赖：
    pip install pandas numpy akshare tqdm openpyxl

并且需要你本地存在 stock_data.py，里面至少需要提供：
    fetch_stock_history
    get_mainboard_stocks

用法：
    python backtest.py
"""

import os
import warnings
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd
import akshare as ak
from tqdm import tqdm

from stock_data import fetch_stock_history

warnings.filterwarnings("ignore")


# ==============================
# 0. 回测参数
# ==============================

# 回测区间
# 注意：
# 1. BACKTEST_START 之前需要有足够历史K线计算 ma120 等指标；
# 2. BACKTEST_END 之后需要至少还有 HOLD_DAYS 个交易日用于卖出。
# BACKTEST_START = "2024-01-01"
# BACKTEST_END = "2026-05-31"
BACKTEST_START = "2026-05-31"
BACKTEST_END = "2026-05-31"
# 持有交易日数
# T 日产生信号，T+1 买入，之后取未来 HOLD_DAYS 根K线：
# 第 1 根是买入日，第 HOLD_DAYS 根是卖出日。
HOLD_DAYS = 20

# 策略参数
MIN_SCORE = 75
POOL_STATUSES = ["强趋势池", "趋势观察池"]

# 指标完整性要求
MIN_BARS = 130

# 流动性门槛：20日平均成交额
MIN_AVG_AMOUNT_20 = 80_000_000

# 股票历史数据获取长度
HISTORY_DAYS = 2000

# 是否强制刷新股票和基准缓存
# 如果你之前缓存过短数据，第一次正式回测建议设为 True。
# 确认缓存已经完整后，可以改为 False，加快后续运行速度。
FORCE_REFRESH_DATA = True

# 买卖价格字段
# 推荐：
#   T 日收盘后出信号；
#   T+1 日 open 买入；
#   持有结束日 close 卖出。
BUY_PRICE_FIELD = "open"
SELL_PRICE_FIELD = "close"

# 交易成本与滑点
BUY_COMMISSION_RATE = 0.0003
SELL_COMMISSION_RATE = 0.0003
STAMP_TAX_RATE = 0.0005
SLIPPAGE_RATE = 0.0005

# 基准
BENCHMARK_SYMBOL = "sh000300"
CACHE_DIR = "data"
OUTPUT_DIR = "output"
BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")


# ==============================
# 0.1 指定日期股票池走势分析参数
# ==============================

# 是否运行历史回测
RUN_BACKTEST = True

# 是否运行指定日期股票池未来走势分析
RUN_FORWARD_ANALYSIS = True

# 要分析的信号日期
# 注意：
# 1. 必须是交易日；
# 2. 该日期之后需要至少 FORWARD_DAYS 个交易日数据。
ANALYSIS_SIGNAL_DATE = "2026-4-20"

# 统计未来多少个交易日
FORWARD_DAYS = 30

# 指定日期分析哪些池
FORWARD_ANALYSIS_STATUSES = ["强趋势池", "趋势观察池"]

# 指定日期分析输出文件名前缀
FORWARD_ANALYSIS_OUTPUT_PREFIX = "pool_forward"


# ==============================
# 1. 工具函数
# ==============================

def max_drawdown(series: pd.Series) -> float:
    """
    计算一段价格或净值序列的最大回撤。
    返回值为负数，例如 -0.15 表示最大回撤 15%。
    """
    s = series.dropna()
    if len(s) == 0:
        return np.nan

    cum_max = s.cummax()
    dd = s / cum_max - 1
    return dd.min()


def get_row_price(row: pd.Series, field: str, fallback: str = "close") -> float:
    """
    从一行行情数据中取价格。
    如果指定字段不存在或为空，则回退到 fallback 字段。
    """
    if field in row.index and not pd.isna(row[field]):
        return float(row[field])

    if fallback in row.index and not pd.isna(row[fallback]):
        return float(row[fallback])

    return np.nan


def calc_trade_return(
    buy_price: float,
    sell_price: float,
    include_cost: bool = True,
) -> float:
    """
    根据买入价和卖出价计算持有期收益。

    如果 include_cost=True，则考虑：
    - 买入佣金；
    - 卖出佣金；
    - 卖出印花税；
    - 买卖滑点。
    """
    if pd.isna(buy_price) or pd.isna(sell_price):
        return np.nan

    if buy_price <= 0 or sell_price <= 0:
        return np.nan

    if not include_cost:
        return sell_price / buy_price - 1

    effective_buy_price = buy_price * (1 + BUY_COMMISSION_RATE + SLIPPAGE_RATE)
    effective_sell_price = sell_price * (
        1 - SELL_COMMISSION_RATE - STAMP_TAX_RATE - SLIPPAGE_RATE
    )

    return effective_sell_price / effective_buy_price - 1


# ==============================
# 2. 指标计算函数
# ==============================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    给单只股票行情数据增加策略所需指标。
    要求 df 至少包含：
        date, open, high, low, close, amount
    """
    df = df.copy()
    df = df.sort_values("date").reset_index(drop=True)

    df["pre_close"] = df["close"].shift(1)

    # 均线
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()

    # 均线斜率
    df["ma20_slope_5"] = df["ma20"] / df["ma20"].shift(5) - 1
    df["ma60_slope_10"] = df["ma60"] / df["ma60"].shift(10) - 1

    # 区间收益
    df["ret10"] = df["close"] / df["close"].shift(10) - 1
    df["ret20"] = df["close"] / df["close"].shift(20) - 1
    df["ret60"] = df["close"] / df["close"].shift(60) - 1

    # 成交额
    df["amount_ma5"] = df["amount"].rolling(5).mean()
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

    # 换手率
    if "turnover" in df.columns:
        df["turnover_ma5"] = df["turnover"].rolling(5).mean()
        df["turnover_ma20"] = df["turnover"].rolling(20).mean()
    else:
        df["turnover_ma5"] = np.nan
        df["turnover_ma20"] = np.nan

    # 新高与距离高点
    df["high20"] = df["high"].rolling(20).max()
    df["high60"] = df["high"].rolling(60).max()
    df["is_20d_high"] = df["close"] >= df["high20"] * 0.999
    df["is_60d_high"] = df["close"] >= df["high60"] * 0.999
    df["dist_to_60d_high"] = df["close"] / df["high60"] - 1

    # 20日最大回撤
    df["max_dd20"] = np.nan
    if len(df) >= 20:
        for i in range(19, len(df)):
            window = df["close"].iloc[i - 19:i + 1]
            df.loc[df.index[i], "max_dd20"] = max_drawdown(window)

    # 上涨日/下跌日成交额比例
    df["is_up_day"] = df["close"] > df["pre_close"]
    df["is_down_day"] = df["close"] < df["pre_close"]

    up_down_ratio_list = []
    for i in range(len(df)):
        if i < 19:
            up_down_ratio_list.append(np.nan)
            continue

        window = df.iloc[i - 19:i + 1]
        up_amount = window.loc[window["is_up_day"], "amount"].sum()
        down_amount = window.loc[window["is_down_day"], "amount"].sum()

        if down_amount <= 0:
            up_down_ratio = np.nan
        else:
            up_down_ratio = up_amount / down_amount

        up_down_ratio_list.append(up_down_ratio)

    df["up_down_amount_ratio20"] = up_down_ratio_list

    # 涨停计数
    if "pct_chg" in df.columns:
        df["is_limit_up"] = df["pct_chg"] >= 9.5
        df["limit_up_count_5"] = df["is_limit_up"].rolling(5).sum()
    else:
        df["is_limit_up"] = False
        df["limit_up_count_5"] = np.nan

    # 放量长阴
    if "pct_chg" in df.columns:
        intraday_range = df["high"] - df["low"]

        close_position = np.where(
            intraday_range > 0,
            (df["close"] - df["low"]) / intraday_range,
            np.nan,
        )

        df["heavy_bearish_candle"] = (
            (df["pct_chg"] <= -5)
            & (df["amount"] > df["amount_ma20"] * 1.5)
            & (close_position <= 0.35)
        )
    else:
        df["heavy_bearish_candle"] = False

    return df


def calc_score(row: pd.Series, bench_ret20: float, bench_ret60: float) -> int:
    """
    根据单只股票某个信号日的指标计算综合评分。
    分数上限为 100。
    """
    score = 0

    # 趋势结构
    if row["close"] > row["ma20"]:
        score += 5
    if row["ma20"] > row["ma60"]:
        score += 8
    if row["ma60"] > row["ma120"]:
        score += 7
    if row["ma20_slope_5"] > 0:
        score += 5
    if row["ma60_slope_10"] > 0:
        score += 5

    # 相对强度
    if row["ret20_rank_pct"] >= 0.70:
        score += 8
    if row["ret60_rank_pct"] >= 0.70:
        score += 8

    if not pd.isna(bench_ret20):
        if row["ret20"] > bench_ret20 + 0.05:
            score += 5
        elif row["ret20"] > bench_ret20:
            score += 3

    if not pd.isna(bench_ret60):
        if row["ret60"] > bench_ret60 + 0.10:
            score += 4
        elif row["ret60"] > bench_ret60:
            score += 2

    # 量能
    ratio = row["amount_ratio_5_20"]

    if 1.2 <= ratio <= 4:
        score += 6
    elif 1.0 <= ratio < 1.2:
        score += 3

    if row["up_down_amount_ratio20"] > 1.2:
        score += 4
    elif row["up_down_amount_ratio20"] > 1.0:
        score += 2

    if (row["is_20d_high"] or row["is_60d_high"]) and ratio >= 1.2:
        score += 3

    if ratio < 4:
        score += 2

    # 突破与高点附近
    if row["is_20d_high"]:
        score += 4
    if row["is_60d_high"]:
        score += 6

    if row["dist_to_60d_high"] >= -0.03:
        score += 3
    elif row["dist_to_60d_high"] >= -0.05:
        score += 2

    if row["close"] > row["ma20"] and row["dist_to_60d_high"] >= -0.05:
        score += 2

    # 回撤控制
    max_dd20 = row["max_dd20"]
    if not pd.isna(max_dd20):
        if max_dd20 > -0.10:
            score += 5
        elif max_dd20 > -0.15:
            score += 3
        elif max_dd20 > -0.20:
            score += 1

    # 过热惩罚
    overheat = False

    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        overheat = True

    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        overheat = True

    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        overheat = True

    if not overheat:
        score += 4

    # 放量长阴过滤
    if not row["heavy_bearish_candle"]:
        score += 3

    # 换手率
    turnover_ma5 = row["turnover_ma5"]

    if pd.isna(turnover_ma5):
        score += 2
    else:
        if turnover_ma5 < 15:
            score += 3
        elif turnover_ma5 < 25:
            score += 1

    return int(min(score, 100))


def is_overheat(row: pd.Series) -> bool:
    """
    判断是否短期过热。
    """
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        return True
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        return True
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        return True
    return False


def classify_status(row: pd.Series) -> str:
    """
    根据筛选结果和评分给股票分类。
    """
    if not row["basic_liquid"]:
        return "流动性不足"

    if row["close"] < row["ma60"]:
        return "跌破MA60剔除"

    if row["close"] < row["ma20"]:
        return "跌破MA20观察"

    if row["heavy_bearish_candle"]:
        return "放量长阴观察"

    if is_overheat(row):
        return "短期过热"

    if row["score"] >= 85 and row["candidate"]:
        return "强趋势池"

    if row["score"] >= MIN_SCORE and row["candidate"]:
        return "趋势观察池"

    if (
        row["close"] > row["ma60"]
        and row["ma20"] > row["ma60"]
        and abs(row["close"] / row["ma20"] - 1) <= 0.05
        and row["ret60_rank_pct"] >= 0.60
    ):
        return "回踩观察"

    return "剔除"


# ==============================
# 3. 股票列表与基准数据
# ==============================

def get_mainboard_stocks() -> pd.DataFrame:
    """
    获取主板股票列表。
    实际逻辑放在 stock_data.py 里。
    """
    from stock_data import get_mainboard_stocks as _get
    return _get()


def fetch_benchmark_data(symbol: str = BENCHMARK_SYMBOL) -> pd.DataFrame:
    """
    获取基准指数数据。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if (not FORCE_REFRESH_DATA) and os.path.exists(BENCHMARK_CACHE_FILE):
        try:
            df = pd.read_csv(BENCHMARK_CACHE_FILE, parse_dates=["date"])
            if not df.empty:
                df = df.sort_values("date").reset_index(drop=True)
                return df
        except Exception:
            pass

    try:
        df = ak.stock_zh_index_daily(symbol=symbol)

        rename_map = {}

        if "日期" in df.columns:
            rename_map["日期"] = "date"
        if "开盘" in df.columns:
            rename_map["开盘"] = "open"
        if "收盘" in df.columns:
            rename_map["收盘"] = "close"
        if "最高" in df.columns:
            rename_map["最高"] = "high"
        if "最低" in df.columns:
            rename_map["最低"] = "low"
        if "成交量" in df.columns:
            rename_map["成交量"] = "volume"
        if "成交额" in df.columns:
            rename_map["成交额"] = "amount"

        if rename_map:
            df = df.rename(columns=rename_map)

        if "date" not in df.columns:
            raise ValueError("基准数据缺少 date 字段")

        if "close" not in df.columns:
            raise ValueError("基准数据缺少 close 字段")

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        df.to_csv(BENCHMARK_CACHE_FILE, index=False, encoding="utf-8-sig")
        return df

    except Exception as e:
        print(f"获取基准数据失败：{e}")
        raise


def get_trading_days(bench_df: pd.DataFrame, start: str, end: str) -> List[datetime]:
    """
    使用基准指数的交易日作为回测交易日历。
    """
    mask = (
        (bench_df["date"] >= pd.Timestamp(start))
        & (bench_df["date"] <= pd.Timestamp(end))
    )
    return bench_df.loc[mask, "date"].tolist()


# ==============================
# 4. 预加载股票数据
# ==============================

def preload_all_stock_data(codes: List[str]) -> Dict[str, pd.DataFrame]:
    """
    加载所有股票历史数据，并预先计算指标。
    """
    data = {}

    print("正在预加载股票历史数据...")

    cache_valid_hours = 0 if FORCE_REFRESH_DATA else 24

    for code in tqdm(codes, desc="股票数据加载"):
        try:
            df = fetch_stock_history(
                code,
                history_days=HISTORY_DAYS,
                data_dir=CACHE_DIR,
                cache_valid_hours=cache_valid_hours,
                adjust="qfq",
                min_bars=MIN_BARS,
                sleep_on_error=0.1,
            )

            if df is None or df.empty:
                continue

            df = df.copy()

            if "date" not in df.columns:
                continue

            required_price_cols = ["open", "high", "low", "close", "amount"]
            missing_cols = [c for c in required_price_cols if c not in df.columns]

            if missing_cols:
                print(f"{code} 缺少字段 {missing_cols}，跳过。")
                continue

            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            if len(df) < MIN_BARS:
                continue

            df = add_indicators(df)

            data[code] = df

        except Exception as e:
            print(f"{code} 加载失败：{e}")
            continue

    print(f"成功加载 {len(data)} 只股票数据。")
    return data


# ==============================
# 5. 单日回测
# ==============================

def backtest_on_date(
    date: datetime,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
) -> List[Dict]:
    """
    对某一个信号日进行回测。
    """
    results = []

    bench_slice = bench_df[bench_df["date"] <= date].copy()

    if len(bench_slice) < 61:
        return results

    bench_close_date = bench_slice.iloc[-1]["close"]
    bench_close_20 = bench_slice.iloc[-21]["close"]
    bench_close_60 = bench_slice.iloc[-61]["close"]

    bench_ret20 = bench_close_date / bench_close_20 - 1
    bench_ret60 = bench_close_date / bench_close_60 - 1

    future_bench = bench_df[bench_df["date"] > date].head(HOLD_DAYS)

    if len(future_bench) < HOLD_DAYS:
        return results

    bench_buy_price = get_row_price(
        future_bench.iloc[0],
        BUY_PRICE_FIELD,
        fallback="close",
    )
    bench_sell_price = get_row_price(
        future_bench.iloc[-1],
        SELL_PRICE_FIELD,
        fallback="close",
    )

    if (
        pd.isna(bench_buy_price)
        or pd.isna(bench_sell_price)
        or bench_buy_price <= 0
        or bench_sell_price <= 0
    ):
        return results

    bench_hold_ret = calc_trade_return(
        bench_buy_price,
        bench_sell_price,
        include_cost=False,
    )

    rows = []

    required_indicator_cols = [
        "close",
        "ma20",
        "ma60",
        "ma120",
        "ma20_slope_5",
        "ma60_slope_10",
        "ret20",
        "ret60",
        "amount_ma20",
        "amount_ratio_5_20",
        "high20",
        "high60",
        "dist_to_60d_high",
        "max_dd20",
        "up_down_amount_ratio20",
    ]

    for code, df in stock_data.items():
        if df is None or df.empty:
            continue

        df_slice = df[df["date"] <= date]

        if len(df_slice) < MIN_BARS:
            continue

        latest = df_slice.iloc[-1]

        if pd.Timestamp(latest["date"]).date() != pd.Timestamp(date).date():
            continue

        has_nan = False

        for col in required_indicator_cols:
            if col not in latest.index or pd.isna(latest[col]):
                has_nan = True
                break

        if has_nan:
            continue

        future_df = df[df["date"] > date].head(HOLD_DAYS)

        if len(future_df) < HOLD_DAYS:
            continue

        buy_row = future_df.iloc[0]
        sell_row = future_df.iloc[-1]

        buy_price = get_row_price(
            buy_row,
            BUY_PRICE_FIELD,
            fallback="close",
        )
        sell_price = get_row_price(
            sell_row,
            SELL_PRICE_FIELD,
            fallback="close",
        )

        if (
            pd.isna(buy_price)
            or pd.isna(sell_price)
            or buy_price <= 0
            or sell_price <= 0
        ):
            continue

        hold_ret_raw = calc_trade_return(
            buy_price,
            sell_price,
            include_cost=False,
        )

        hold_ret = calc_trade_return(
            buy_price,
            sell_price,
            include_cost=True,
        )

        row_dict = {
            "code": code,

            "signal_date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "buy_date": pd.Timestamp(buy_row["date"]).strftime("%Y-%m-%d"),
            "sell_date": pd.Timestamp(sell_row["date"]).strftime("%Y-%m-%d"),

            "close": latest["close"],
            "ma20": latest["ma20"],
            "ma60": latest["ma60"],
            "ma120": latest["ma120"],
            "ma20_slope_5": latest["ma20_slope_5"],
            "ma60_slope_10": latest["ma60_slope_10"],

            "ret10": latest["ret10"],
            "ret20": latest["ret20"],
            "ret60": latest["ret60"],

            "amount_ma5": latest["amount_ma5"],
            "amount_ma20": latest["amount_ma20"],
            "amount_ratio_5_20": latest["amount_ratio_5_20"],

            "turnover_ma5": latest.get("turnover_ma5", np.nan),
            "turnover_ma20": latest.get("turnover_ma20", np.nan),

            "high20": latest["high20"],
            "high60": latest["high60"],
            "is_20d_high": bool(latest["is_20d_high"]),
            "is_60d_high": bool(latest["is_60d_high"]),
            "dist_to_60d_high": latest["dist_to_60d_high"],

            "max_dd20": latest["max_dd20"],
            "up_down_amount_ratio20": latest["up_down_amount_ratio20"],

            "limit_up_count_5": latest.get("limit_up_count_5", np.nan),
            "heavy_bearish_candle": bool(latest.get("heavy_bearish_candle", False)),

            "buy_price": buy_price,
            "sell_price": sell_price,
            "hold_ret_raw": hold_ret_raw,
            "hold_ret": hold_ret,

            "bench_ret20": bench_ret20,
            "bench_ret60": bench_ret60,
            "bench_hold_ret": bench_hold_ret,
        }

        rows.append(row_dict)

    if not rows:
        return results   
    
    df_pool = pd.DataFrame(rows)

    df_pool["ret20_rank_pct"] = df_pool["ret20"].rank(pct=True)
    df_pool["ret60_rank_pct"] = df_pool["ret60"].rank(pct=True)

    df_pool["basic_liquid"] = df_pool["amount_ma20"] >= MIN_AVG_AMOUNT_20

    df_pool["trend_basic"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["ma20"] > df_pool["ma60"])
        & (df_pool["ma60"] > df_pool["ma120"])
        & (df_pool["ma20_slope_5"] > 0)
        & (df_pool["ma60_slope_10"] > 0)
    )

    df_pool["relative_strength"] = (
        (df_pool["ret20_rank_pct"] >= 0.70)
        & (df_pool["ret60_rank_pct"] >= 0.70)
    )

    df_pool["volume_ok"] = (
        (df_pool["amount_ratio_5_20"] >= 1.2)
        & (df_pool["amount_ratio_5_20"] <= 4)
    )

    df_pool["near_breakout"] = (
        (df_pool["dist_to_60d_high"] >= -0.05)
        | df_pool["is_20d_high"]
        | df_pool["is_60d_high"]
    )

    df_pool["risk_ok"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["max_dd20"] > -0.20)
        & (~df_pool["heavy_bearish_candle"])
    )

    df_pool["candidate"] = (
        df_pool["basic_liquid"]
        & df_pool["trend_basic"]
        & df_pool["relative_strength"]
        & df_pool["volume_ok"]
        & df_pool["near_breakout"]
        & df_pool["risk_ok"]
    )

    df_pool["score"] = df_pool.apply(
        lambda row: calc_score(row, bench_ret20, bench_ret60),
        axis=1,
    )

    df_pool["status"] = df_pool.apply(classify_status, axis=1)

    selected = df_pool[df_pool["status"].isin(POOL_STATUSES)].copy()

    if selected.empty:
        return results

    selected = selected.sort_values("score", ascending=False)

    for _, stock in selected.iterrows():
        results.append({
            "signal_date": stock["signal_date"],
            "buy_date": stock["buy_date"],
            "sell_date": stock["sell_date"],

            "code": stock["code"],
            "status": stock["status"],
            "score": stock["score"],

            "buy_price": stock["buy_price"],
            "sell_price": stock["sell_price"],

            "hold_ret_raw": stock["hold_ret_raw"],
            "hold_ret": stock["hold_ret"],

            "bench_hold_ret": stock["bench_hold_ret"],
            "excess_ret": stock["hold_ret"] - stock["bench_hold_ret"],

            "ret20": stock["ret20"],
            "ret60": stock["ret60"],
            "ret20_rank_pct": stock["ret20_rank_pct"],
            "ret60_rank_pct": stock["ret60_rank_pct"],

            "amount_ma20": stock["amount_ma20"],
            "amount_ratio_5_20": stock["amount_ratio_5_20"],

            "max_dd20": stock["max_dd20"],
            "dist_to_60d_high": stock["dist_to_60d_high"],
        })

    return results


# ==============================
# 5.1 指定日期股票池构建
# ==============================

def build_code_name_map(universe: pd.DataFrame) -> Dict[str, str]:
    """
    从股票列表中构建 code -> name 的映射。
    兼容不同 stock_data.py 返回的字段名。
    """
    if universe is None or universe.empty:
        return {}

    name_col = None

    for c in ["name", "名称", "stock_name", "股票简称"]:
        if c in universe.columns:
            name_col = c
            break

    if name_col is None or "code" not in universe.columns:
        return {}

    tmp = universe[["code", name_col]].copy()
    tmp["code"] = tmp["code"].astype(str)

    return dict(zip(tmp["code"], tmp[name_col].astype(str)))


def build_pool_on_date(
    date: datetime,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    code_name_map: Dict[str, str] = None,
) -> pd.DataFrame:
    """
    在指定日期 date，对所有股票进行分类。

    返回：
        当日全市场股票池 DataFrame。
    """
    if code_name_map is None:
        code_name_map = {}

    bench_slice = bench_df[bench_df["date"] <= date].copy()

    if len(bench_slice) < 61:
        return pd.DataFrame()

    bench_close_date = bench_slice.iloc[-1]["close"]
    bench_close_20 = bench_slice.iloc[-21]["close"]
    bench_close_60 = bench_slice.iloc[-61]["close"]

    bench_ret20 = bench_close_date / bench_close_20 - 1
    bench_ret60 = bench_close_date / bench_close_60 - 1

    required_indicator_cols = [
        "close",
        "ma20",
        "ma60",
        "ma120",
        "ma20_slope_5",
        "ma60_slope_10",
        "ret20",
        "ret60",
        "amount_ma20",
        "amount_ratio_5_20",
        "high20",
        "high60",
        "dist_to_60d_high",
        "max_dd20",
        "up_down_amount_ratio20",
    ]

    rows = []

    for code, df in stock_data.items():
        if df is None or df.empty:
            continue

        df_slice = df[df["date"] <= date]

        if len(df_slice) < MIN_BARS:
            continue

        latest = df_slice.iloc[-1]

        # 要求信号日当天股票有交易数据，避免停牌股票用旧K线生成信号。
        if pd.Timestamp(latest["date"]).date() != pd.Timestamp(date).date():
            continue

        has_nan = False

        for col in required_indicator_cols:
            if col not in latest.index or pd.isna(latest[col]):
                has_nan = True
                break

        if has_nan:
            continue

        rows.append({
            "code": code,
            "name": code_name_map.get(str(code), ""),
            "signal_date": pd.Timestamp(date).strftime("%Y-%m-%d"),

            "close": latest["close"],
            "ma20": latest["ma20"],
            "ma60": latest["ma60"],
            "ma120": latest["ma120"],
            "ma20_slope_5": latest["ma20_slope_5"],
            "ma60_slope_10": latest["ma60_slope_10"],

            "ret10": latest["ret10"],
            "ret20": latest["ret20"],
            "ret60": latest["ret60"],

            "amount_ma5": latest["amount_ma5"],
            "amount_ma20": latest["amount_ma20"],
            "amount_ratio_5_20": latest["amount_ratio_5_20"],

            "turnover_ma5": latest.get("turnover_ma5", np.nan),
            "turnover_ma20": latest.get("turnover_ma20", np.nan),

            "high20": latest["high20"],
            "high60": latest["high60"],
            "is_20d_high": bool(latest["is_20d_high"]),
            "is_60d_high": bool(latest["is_60d_high"]),
            "dist_to_60d_high": latest["dist_to_60d_high"],

            "max_dd20": latest["max_dd20"],
            "up_down_amount_ratio20": latest["up_down_amount_ratio20"],

            "limit_up_count_5": latest.get("limit_up_count_5", np.nan),
            "heavy_bearish_candle": bool(latest.get("heavy_bearish_candle", False)),

            "bench_ret20": bench_ret20,
            "bench_ret60": bench_ret60,
        })

    if not rows:
        return pd.DataFrame()

    df_pool = pd.DataFrame(rows)

    df_pool["ret20_rank_pct"] = df_pool["ret20"].rank(pct=True)
    df_pool["ret60_rank_pct"] = df_pool["ret60"].rank(pct=True)

    df_pool["basic_liquid"] = df_pool["amount_ma20"] >= MIN_AVG_AMOUNT_20

    df_pool["trend_basic"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["ma20"] > df_pool["ma60"])
        & (df_pool["ma60"] > df_pool["ma120"])
        & (df_pool["ma20_slope_5"] > 0)
        & (df_pool["ma60_slope_10"] > 0)
    )

    df_pool["relative_strength"] = (
        (df_pool["ret20_rank_pct"] >= 0.70)
        & (df_pool["ret60_rank_pct"] >= 0.70)
    )

    df_pool["volume_ok"] = (
        (df_pool["amount_ratio_5_20"] >= 1.2)
        & (df_pool["amount_ratio_5_20"] <= 4)
    )

    df_pool["near_breakout"] = (
        (df_pool["dist_to_60d_high"] >= -0.05)
        | df_pool["is_20d_high"]
        | df_pool["is_60d_high"]
    )

    df_pool["risk_ok"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["max_dd20"] > -0.20)
        & (~df_pool["heavy_bearish_candle"])
    )

    df_pool["candidate"] = (
        df_pool["basic_liquid"]
        & df_pool["trend_basic"]
        & df_pool["relative_strength"]
        & df_pool["volume_ok"]
        & df_pool["near_breakout"]
        & df_pool["risk_ok"]
    )

    df_pool["score"] = df_pool.apply(
        lambda row: calc_score(row, bench_ret20, bench_ret60),
        axis=1,
    )

    df_pool["status"] = df_pool.apply(classify_status, axis=1)

    df_pool = df_pool.sort_values(
        ["status", "score"],
        ascending=[True, False],
    ).reset_index(drop=True)

    return df_pool


# ==============================
# 5.2 未来 1~N 日走势计算
# ==============================

def calc_forward_return_path(
    df: pd.DataFrame,
    signal_date: datetime,
    forward_days: int = 30,
    buy_price_field: str = BUY_PRICE_FIELD,
) -> Dict:
    """
    计算某只股票或指数，在 signal_date 后未来 1~forward_days 个交易日的累计收益路径。

    口径：
        signal_date 收盘后产生信号；
        下一交易日开盘价作为买入基准；
        D1 ~ Dn 使用对应交易日收盘价相对买入价的累计收益。
    """
    if df is None or df.empty:
        return {}

    future_df = df[df["date"] > signal_date].head(forward_days).copy()

    if future_df.empty:
        return {}

    # 为了 D1~D30 可比，要求未来数据完整。
    if len(future_df) < forward_days:
        return {}

    buy_row = future_df.iloc[0]
    buy_price = get_row_price(buy_row, buy_price_field, fallback="close")

    if pd.isna(buy_price) or buy_price <= 0:
        return {}

    result = {
        "buy_date": pd.Timestamp(buy_row["date"]).strftime("%Y-%m-%d"),
    }

    returns = []

    for i in range(forward_days):
        row = future_df.iloc[i]
        close_price = get_row_price(row, "close", fallback="close")

        if pd.isna(close_price) or close_price <= 0:
            ret = np.nan
        else:
            ret = close_price / buy_price - 1

        result[f"D{i + 1}"] = ret
        returns.append(ret)

    ret_series = pd.Series(returns).dropna()

    if len(ret_series) == 0:
        result[f"max_ret_{forward_days}"] = np.nan
        result[f"min_ret_{forward_days}"] = np.nan
        result[f"max_dd_{forward_days}"] = np.nan
        result["day_to_max_ret"] = np.nan
        result["first_profit_5_day"] = np.nan
        result["first_profit_10_day"] = np.nan
        result["first_drawdown_5_day"] = np.nan
        return result

    result[f"max_ret_{forward_days}"] = ret_series.max()
    result[f"min_ret_{forward_days}"] = ret_series.min()

    nav = 1 + ret_series
    result[f"max_dd_{forward_days}"] = max_drawdown(nav)

    result["day_to_max_ret"] = int(ret_series.idxmax() + 1)

    first_profit_5 = np.nan
    first_profit_10 = np.nan
    first_drawdown_5 = np.nan

    for idx, ret in enumerate(returns, start=1):
        if pd.isna(ret):
            continue

        if pd.isna(first_profit_5) and ret >= 0.05:
            first_profit_5 = idx

        if pd.isna(first_profit_10) and ret >= 0.10:
            first_profit_10 = idx

        if pd.isna(first_drawdown_5) and ret <= -0.05:
            first_drawdown_5 = idx

    result["first_profit_5_day"] = first_profit_5
    result["first_profit_10_day"] = first_profit_10
    result["first_drawdown_5_day"] = first_drawdown_5

    return result


def build_forward_summary(
    detail_df: pd.DataFrame,
    forward_days: int,
    group_col: str = "status",
) -> pd.DataFrame:
    """
    根据走势明细构建分组统计表。
    """
    if detail_df is None or detail_df.empty:
        return pd.DataFrame()

    bench_rows = detail_df[detail_df["row_type"] == "benchmark"]
    stock_rows = detail_df[detail_df["row_type"] == "stock"].copy()

    if bench_rows.empty or stock_rows.empty:
        return pd.DataFrame()

    bench_row = bench_rows.iloc[0]
    summary_rows = []

    for group_value, group in stock_rows.groupby(group_col):
        for h in range(1, forward_days + 1):
            col = f"D{h}"

            if col not in group.columns:
                continue

            bench_ret = bench_row[col]
            valid = group[group[col].notna()].copy()

            if valid.empty:
                continue

            ret = valid[col]
            excess = ret - bench_ret

            summary_rows.append({
                group_col: group_value,
                "horizon": h,
                "count": len(valid),
                "avg_ret": ret.mean(),
                "median_ret": ret.median(),
                "win_rate": (ret > 0).mean(),
                "bench_ret": bench_ret,
                "avg_excess": excess.mean(),
                "outperform_rate": (excess > 0).mean(),
            })

    return pd.DataFrame(summary_rows)


def build_overall_forward_summary(
    detail_df: pd.DataFrame,
    forward_days: int,
) -> pd.DataFrame:
    """
    构建不分组的整体统计。
    """
    if detail_df is None or detail_df.empty:
        return pd.DataFrame()

    bench_rows = detail_df[detail_df["row_type"] == "benchmark"]
    stock_rows = detail_df[detail_df["row_type"] == "stock"].copy()

    if bench_rows.empty or stock_rows.empty:
        return pd.DataFrame()

    bench_row = bench_rows.iloc[0]
    rows = []

    for h in range(1, forward_days + 1):
        col = f"D{h}"

        if col not in stock_rows.columns:
            continue

        bench_ret = bench_row[col]
        valid = stock_rows[stock_rows[col].notna()].copy()

        if valid.empty:
            continue

        ret = valid[col]
        excess = ret - bench_ret

        rows.append({
            "horizon": h,
            "count": len(valid),
            "avg_ret": ret.mean(),
            "median_ret": ret.median(),
            "win_rate": (ret > 0).mean(),
            "bench_ret": bench_ret,
            "avg_excess": excess.mean(),
            "outperform_rate": (excess > 0).mean(),
        })

    return pd.DataFrame(rows)


def build_excess_detail(
    detail_df: pd.DataFrame,
    forward_days: int,
) -> pd.DataFrame:
    """
    构建每只股票相对基准的超额收益明细表。
    """
    if detail_df is None or detail_df.empty:
        return pd.DataFrame()

    bench_rows = detail_df[detail_df["row_type"] == "benchmark"]
    stock_rows = detail_df[detail_df["row_type"] == "stock"].copy()

    if bench_rows.empty or stock_rows.empty:
        return pd.DataFrame()

    bench_row = bench_rows.iloc[0]
    rows = []

    for _, row in stock_rows.iterrows():
        item = {
            "code": row["code"],
            "name": row.get("name", ""),
            "status": row["status"],
            "score": row["score"],
            "signal_date": row["signal_date"],
            "buy_date": row["buy_date"],
        }

        outperform_days = 0
        valid_days = 0
        excess_values = []

        for h in range(1, forward_days + 1):
            col = f"D{h}"
            stock_ret = row[col]
            bench_ret = bench_row[col]

            if pd.isna(stock_ret) or pd.isna(bench_ret):
                excess = np.nan
            else:
                excess = stock_ret - bench_ret
                valid_days += 1
                excess_values.append(excess)

                if excess > 0:
                    outperform_days += 1

            item[f"excess_D{h}"] = excess

        item["avg_excess"] = np.nan if len(excess_values) == 0 else np.mean(excess_values)
        item["outperform_days"] = outperform_days
        item["valid_days"] = valid_days

        rows.append(item)

    return pd.DataFrame(rows)


# ==============================
# 5.3 指定日期股票池未来走势分析
# ==============================

def analyze_pool_forward_returns(
    signal_date: str,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    code_name_map: Dict[str, str] = None,
    forward_days: int = 30,
    statuses: List[str] = None,
) -> None:
    """
    指定某个信号日，分析当日强趋势池/趋势观察池未来 1~N 个交易日走势。

    输出：
        output/pool_forward_YYYY-MM-DD.xlsx

    Sheet：
        1. 走势明细
        2. 超额收益
        3. 分组统计
        4. 整体统计
        5. 入选股票
        6. 全市场分类
    """
    if statuses is None:
        statuses = FORWARD_ANALYSIS_STATUSES

    if code_name_map is None:
        code_name_map = {}

    signal_dt = pd.Timestamp(signal_date)

    print("\n========== 指定日期股票池走势分析 ==========")
    print(f"信号日期: {signal_dt.date()}")
    print(f"分析池: {statuses}")
    print(f"未来交易日数: {forward_days}")
    print("==========================================")

    pool_df = build_pool_on_date(
        date=signal_dt,
        stock_data=stock_data,
        bench_df=bench_df,
        code_name_map=code_name_map,
    )

    if pool_df.empty:
        print("指定日期未能构建股票池。可能原因：")
        print("1. 当天不是交易日；")
        print("2. 数据不足；")
        print("3. 股票在当天没有交易数据。")
        return

    selected_df = pool_df[pool_df["status"].isin(statuses)].copy()
    selected_df = selected_df.sort_values("score", ascending=False).reset_index(drop=True)

    if selected_df.empty:
        print(f"{signal_date} 没有股票进入 {statuses}。")

        output_all_pool = os.path.join(
            OUTPUT_DIR,
            f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_dt.strftime('%Y-%m-%d')}_all_pool.csv",
        )

        pool_df.to_csv(output_all_pool, index=False, encoding="utf-8-sig")
        print(f"已保存当日全市场分类到：{output_all_pool}")
        return

    print(f"入选股票数量: {len(selected_df)}")

    preview_cols = [
        "code",
        "name",
        "status",
        "score",
        "ret20",
        "ret60",
        "amount_ma20",
    ]

    preview_cols = [c for c in preview_cols if c in selected_df.columns]

    print(selected_df[preview_cols].head(20).to_string(index=False))

    # 基准未来走势
    bench_path = calc_forward_return_path(
        df=bench_df,
        signal_date=signal_dt,
        forward_days=forward_days,
        buy_price_field=BUY_PRICE_FIELD,
    )

    if not bench_path:
        print("基准未来数据不足，无法计算未来走势。")
        return

    detail_rows = []

    # 第一行放大盘
    bench_row = {
        "row_type": "benchmark",
        "code": "沪深300",
        "name": "沪深300",
        "status": "benchmark",
        "score": np.nan,
        "signal_date": signal_dt.strftime("%Y-%m-%d"),
    }

    bench_row.update(bench_path)
    detail_rows.append(bench_row)

    skipped = 0

    for _, stock in selected_df.iterrows():
        code = stock["code"]

        if code not in stock_data:
            skipped += 1
            continue

        df = stock_data[code]

        path = calc_forward_return_path(
            df=df,
            signal_date=signal_dt,
            forward_days=forward_days,
            buy_price_field=BUY_PRICE_FIELD,
        )

        if not path:
            skipped += 1
            continue

        row = {
            "row_type": "stock",
            "code": code,
            "name": stock.get("name", code_name_map.get(str(code), "")),
            "status": stock["status"],
            "score": stock["score"],
            "signal_date": signal_dt.strftime("%Y-%m-%d"),
            "close_on_signal": stock["close"],
            "ret20": stock["ret20"],
            "ret60": stock["ret60"],
            "ret20_rank_pct": stock["ret20_rank_pct"],
            "ret60_rank_pct": stock["ret60_rank_pct"],
            "amount_ma20": stock["amount_ma20"],
            "amount_ratio_5_20": stock["amount_ratio_5_20"],
            "max_dd20": stock["max_dd20"],
            "dist_to_60d_high": stock["dist_to_60d_high"],
        }

        row.update(path)
        detail_rows.append(row)

    detail_df = pd.DataFrame(detail_rows)

    if len(detail_df) <= 1:
        print("入选股票未来数据不足，无法生成走势分析。")
        return

    if skipped > 0:
        print(f"有 {skipped} 只股票由于未来数据不足或价格异常被跳过。")

    excess_df = build_excess_detail(
        detail_df=detail_df,
        forward_days=forward_days,
    )

    group_summary_df = build_forward_summary(
        detail_df=detail_df,
        forward_days=forward_days,
        group_col="status",
    )

    overall_summary_df = build_overall_forward_summary(
        detail_df=detail_df,
        forward_days=forward_days,
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_xlsx = os.path.join(
        OUTPUT_DIR,
        f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_dt.strftime('%Y-%m-%d')}.xlsx",
    )

    output_csv = os.path.join(
        OUTPUT_DIR,
        f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_dt.strftime('%Y-%m-%d')}_detail.csv",
    )

    try:
        with pd.ExcelWriter(output_xlsx) as writer:
            detail_df.to_excel(writer, sheet_name="走势明细", index=False)
            excess_df.to_excel(writer, sheet_name="超额收益", index=False)
            group_summary_df.to_excel(writer, sheet_name="分组统计", index=False)
            overall_summary_df.to_excel(writer, sheet_name="整体统计", index=False)
            selected_df.to_excel(writer, sheet_name="入选股票", index=False)
            pool_df.to_excel(writer, sheet_name="全市场分类", index=False)

        print(f"\n指定日期走势分析已保存到：{output_xlsx}")

    except Exception as e:
        print(f"保存 Excel 失败：{e}")
        print("改为保存 CSV 文件。")

        detail_df.to_csv(output_csv, index=False, encoding="utf-8-sig")

        selected_csv = os.path.join(
            OUTPUT_DIR,
            f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_dt.strftime('%Y-%m-%d')}_selected.csv",
        )

        selected_df.to_csv(selected_csv, index=False, encoding="utf-8-sig")

        group_csv = os.path.join(
            OUTPUT_DIR,
            f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_dt.strftime('%Y-%m-%d')}_group_summary.csv",
        )

        group_summary_df.to_csv(group_csv, index=False, encoding="utf-8-sig")

        print(f"走势明细已保存到：{output_csv}")
        print(f"入选股票已保存到：{selected_csv}")
        print(f"分组统计已保存到：{group_csv}")

    print("\n========== 未来走势摘要 ==========")

    key_horizons = [1, 3, 5, 10, 20, 30]
    key_horizons = [h for h in key_horizons if h <= forward_days]

    if not overall_summary_df.empty:
        display_df = overall_summary_df[
            overall_summary_df["horizon"].isin(key_horizons)
        ].copy()

        if not display_df.empty:
            print("\n整体统计：")
            print(display_df.to_string(index=False, formatters={
                "avg_ret": "{:.2%}".format,
                "median_ret": "{:.2%}".format,
                "win_rate": "{:.2%}".format,
                "bench_ret": "{:.2%}".format,
                "avg_excess": "{:.2%}".format,
                "outperform_rate": "{:.2%}".format,
            }))

    if not group_summary_df.empty:
        display_group_df = group_summary_df[
            group_summary_df["horizon"].isin(key_horizons)
        ].copy()

        if not display_group_df.empty:
            print("\n分组统计：")
            print(display_group_df.to_string(index=False, formatters={
                "avg_ret": "{:.2%}".format,
                "median_ret": "{:.2%}".format,
                "win_rate": "{2%}".format,
                "bench_ret": "{:.2%}".format,
                "avg_excess": "{:.2%}".format,
                "outperform_rate": "{:.2%}".format,
            }))


# ==============================
# 6. 回测统计
# ==============================

def print_and_save_statistics(trades_df: pd.DataFrame) -> None:
    """
    输出并保存回测统计。
    """
    trades_df = trades_df.copy()

    trades_df = trades_df.sort_values(
        ["signal_date", "score"],
        ascending=[True, False],
    ).reset_index(drop=True)

    trades_file = os.path.join(OUTPUT_DIR, "backtest_trades.csv")
    trades_df.to_csv(trades_file, index=False, encoding="utf-8-sig")

    print(f"\n共产生 {len(trades_df)} 次交易，已保存到：{trades_file}")

    print("\n========== 单笔交易统计 ==========")

    avg_ret = trades_df["hold_ret"].mean()
    avg_ret_raw = trades_df["hold_ret_raw"].mean()
    win_rate = (trades_df["hold_ret"] > 0).mean()
    avg_excess = trades_df["excess_ret"].mean()

    print(f"交易总次数: {len(trades_df)}")
    print(f"单笔平均收益，不扣成本: {avg_ret_raw:.4%}")
    print(f"单笔平均收益，扣成本后: {avg_ret:.4%}")
    print(f"单笔胜率，扣成本后: {win_rate:.2%}")
    print(f"单笔平均超额收益，相对沪深300: {avg_excess:.4%}")

    print("\n========== 按信号日等权统计 ==========")

    daily_signal = trades_df.groupby("signal_date").agg(
        选股数量=("code", "count"),
        平均收益不扣成本=("hold_ret_raw", "mean"),
        平均收益扣成本=("hold_ret", "mean"),
        胜率=("hold_ret", lambda x: (x > 0).mean()),
        平均超额=("excess_ret", "mean"),
        基准收益=("bench_hold_ret", "mean"),
    ).reset_index()

    print(f"信号日数量: {len(daily_signal)}")
    print(f"平均每日选股数量: {daily_signal['选股数量'].mean():.2f}")
    print(f"信号日平均收益，不扣成本: {daily_signal['平均收益不扣成本'].mean():.4%}")
    print(f"信号日平均收益，扣成本后: {daily_signal['平均收益扣成本'].mean():.4%}")
    print(f"信号日平均胜率: {daily_signal['胜率'].mean():.2%}")
    print(f"信号日平均超额: {daily_signal['平均超额'].mean():.4%}")

    signal_curve = (1 + daily_signal["平均收益扣成本"]).cumprod()
    signal_total_return = signal_curve.iloc[-1] - 1
    signal_max_dd = (signal_curve / signal_curve.cummax() - 1).min()

    print(f"信号批次连乘收益，非严格资金曲线: {signal_total_return:.4%}")
    print(f"信号批次最大回撤，非严格资金曲线: {signal_max_dd:.4%}")

    daily_file = os.path.join(OUTPUT_DIR, "backtest_daily_signal.csv")
    daily_signal.to_csv(daily_file, index=False, encoding="utf-8-sig")

    print(f"按信号日统计已保存到：{daily_file}")

    print("\n========== 年度统计 ==========")

    trades_df["year"] = pd.to_datetime(trades_df["signal_date"]).dt.year

    yearly = trades_df.groupby("year").agg(
        交易次数=("code", "count"),
        平均收益不扣成本=("hold_ret_raw", "mean"),
        平均收益扣成本=("hold_ret", "mean"),
        胜率=("hold_ret", lambda x: (x > 0).mean()),
        平均超额=("excess_ret", "mean"),
    )

    print(yearly.to_string())

    yearly_file = os.path.join(OUTPUT_DIR, "backtest_yearly.csv")
    yearly.to_csv(yearly_file, encoding="utf-8-sig")

    print(f"年度统计已保存到：{yearly_file}")


# ==============================
# 7. 主流程
# ==============================

def run_backtest() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("========== A股趋势策略历史回测 ==========")
    print(f"回测区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"持有交易日数: {HOLD_DAYS}")
    print(f"最低评分: {MIN_SCORE}")
    print(f"目标池: {POOL_STATUSES}")
    print(f"是否强制刷新数据: {FORCE_REFRESH_DATA}")
    print(f"是否运行历史回测: {RUN_BACKTEST}")
    print(f"是否运行指定日期走势分析: {RUN_FORWARD_ANALYSIS}")
    print("========================================")

    print("\n获取股票列表...")
    universe = get_mainboard_stocks()

    if universe is None or universe.empty:
        print("股票列表为空，回测终止。")
        return

    if "code" not in universe.columns:
        print("股票列表缺少 code 字段，回测终止。")
        return

    codes = universe["code"].astype(str).tolist()
    print(f"股票数量: {len(codes)}")

    code_name_map = build_code_name_map(universe)

    print("\n加载基准数据...")
    bench_df = fetch_benchmark_data()
    bench_df = bench_df.sort_values("date").reset_index(drop=True)

    trading_days = get_trading_days(
        bench_df,
        BACKTEST_START,
        BACKTEST_END,
    )

    print(f"回测区间交易日数: {len(trading_days)}")

    print("\n预加载股票历史数据...")
    all_stock_data = preload_all_stock_data(codes)

    if not all_stock_data:
        print("没有成功加载任何股票数据，回测终止。")
        return

    # ==============================
    # 指定日期股票池未来走势分析
    # ==============================
    if RUN_FORWARD_ANALYSIS:
        analyze_pool_forward_returns(
            signal_date=ANALYSIS_SIGNAL_DATE,
            stock_data=all_stock_data,
            bench_df=bench_df,
            code_name_map=code_name_map,
            forward_days=FORWARD_DAYS,
            statuses=FORWARD_ANALYSIS_STATUSES,
        )

    # 如果不运行历史回测，到这里结束
    if not RUN_BACKTEST:
        print("\nRUN_BACKTEST=False，跳过历史回测。")
        return

    if len(trading_days) == 0:
        print("回测区间内没有交易日，回测终止。")
        return

    all_trades = []

    print("\n开始历史回测...")
    for date in tqdm(trading_days, desc="回测进度"):
        try:
            trades = backtest_on_date(
                date=date,
                stock_data=all_stock_data,
                bench_df=bench_df,
            )
            all_trades.extend(trades)

        except Exception as e:
            print(f"日期 {pd.Timestamp(date).date()} 回测出错：{e}")

    if not all_trades:
        print("\n回测未产生任何交易。")
        print("可能原因：")
        print("1. 策略条件过严；")
        print("2. 数据仍然不完整；")
        print("3. BACKTEST_START 过早，前期指标不足；")
        print("4. BACKTEST_END 之后没有足够未来交易日用于卖出；")
        print("5. 股票池数量或主板列表获取异常。")
        return

    trades_df = pd.DataFrame(all_trades)

    print_and_save_statistics(trades_df)


if __name__ == "__main__":
    run_backtest()