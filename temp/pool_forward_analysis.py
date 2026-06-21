"""
A股趋势策略：指定日期股票池未来走势分析

功能：
1. 指定一个信号日期 signal_date；
2. 在该日期对所有股票进行策略分类；
3. 找出该日的 ["强趋势池", "趋势观察池"]；
4. 使用 signal_date 后第一个交易日开盘价作为买入价；
5. 统计未来 1~N 个交易日的收盘累计收益；
6. 第一行放沪深300走势，方便对比；
7. 输出 Excel 文件。

依赖：
    pip install pandas numpy akshare tqdm openpyxl

需要本地存在 stock_data.py，并提供：
    fetch_stock_history
    get_mainboard_stocks

用法示例：
    python pool_forward_analysis.py --date 2026-04-30 --days 30

强制刷新数据：
    python pool_forward_analysis.py --date 2026-04-30 --days 30 --refresh

如果日期不是交易日，自动使用前一个交易日：
    python pool_forward_analysis.py --date 2026-05-01 --days 30 --use-prev-trading-day
"""

import os
import argparse
import warnings
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import akshare as ak
from tqdm import tqdm

from stock_data import fetch_stock_history

warnings.filterwarnings("ignore")


# ==============================
# 0. 默认参数
# ==============================

CACHE_DIR = "data"
OUTPUT_DIR = "output"

BENCHMARK_SYMBOL = "sh000300"
BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

HISTORY_DAYS = 2000
FORCE_REFRESH_DATA = False

MIN_SCORE = 75
POOL_STATUSES = ["强趋势池", "趋势观察池"]

MIN_BARS = 130
MIN_AVG_AMOUNT_20 = 80_000_000

BUY_PRICE_FIELD = "open"

FORWARD_ANALYSIS_OUTPUT_PREFIX = "pool_forward"


# ==============================
# 1. 工具函数
# ==============================

def max_drawdown(series: pd.Series) -> float:
    """
    计算最大回撤。
    输入可以是价格序列，也可以是净值序列。
    返回负数，例如 -0.12 表示最大回撤 12%。
    """
    s = series.dropna()
    if len(s) == 0:
        return np.nan

    cum_max = s.cummax()
    dd = s / cum_max - 1
    return dd.min()


def get_row_price(row: pd.Series, field: str, fallback: str = "close") -> float:
    """
    从一行行情数据中获取价格。
    如果指定字段不存在或为空，则使用 fallback 字段。
    """
    if field in row.index and not pd.isna(row[field]):
        return float(row[field])

    if fallback in row.index and not pd.isna(row[fallback]):
        return float(row[fallback])

    return np.nan


def normalize_date_col(df: pd.DataFrame) -> pd.DataFrame:
    """
    统一 date 字段为 pandas Timestamp，并去掉时分秒。
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return df


# ==============================
# 2. 指标计算
# ==============================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    给单只股票行情数据增加策略指标。
    """

    df = df.copy()
    df = normalize_date_col(df)

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

    # 新高
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
    计算趋势评分。
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

    # 突破和高点附近
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

    # 过热
    overheat = False

    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        overheat = True

    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        overheat = True

    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        overheat = True

    if not overheat:
        score += 4

    # 放量长阴
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
    根据条件和评分分类。
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
# 3. 数据加载
# ==============================

def get_mainboard_stocks() -> pd.DataFrame:
    """
    获取主板股票列表。
    实际实现在 stock_data.py 中。
    """
    from stock_data import get_mainboard_stocks as _get
    return _get()


def fetch_benchmark_data(symbol: str = BENCHMARK_SYMBOL) -> pd.DataFrame:
    """
    获取基准指数数据。
    默认沪深300。
    """

    os.makedirs(CACHE_DIR, exist_ok=True)

    if (not FORCE_REFRESH_DATA) and os.path.exists(BENCHMARK_CACHE_FILE):
        try:
            df = pd.read_csv(BENCHMARK_CACHE_FILE, parse_dates=["date"])
            if not df.empty:
                df = normalize_date_col(df)
                return df
        except Exception:
            pass

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

    df = normalize_date_col(df)

    df.to_csv(BENCHMARK_CACHE_FILE, index=False, encoding="utf-8-sig")

    return df


def preload_all_stock_data(codes: List[str]) -> Dict[str, pd.DataFrame]:
    """
    加载所有股票历史数据，并预先计算指标。
    """

    data = {}

    print("正在预加载股票历史数据...")

    # cache_valid_hours = 0 if FORCE_REFRESH_DATA else 24
    cache_valid_hours = 0 if FORCE_REFRESH_DATA else 9999

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

            df = normalize_date_col(df)

            if len(df) < MIN_BARS:
                continue

            df = add_indicators(df)

            data[str(code)] = df

        except Exception as e:
            print(f"{code} 加载失败：{e}")
            continue

    print(f"成功加载 {len(data)} 只股票数据。")

    return data


def build_code_name_map(universe: pd.DataFrame) -> Dict[str, str]:
    """
    从股票列表构建 code -> name 映射。
    """

    if universe is None or universe.empty:
        return {}

    if "code" not in universe.columns:
        return {}

    name_col = None

    for c in ["name", "名称", "stock_name", "股票简称"]:
        if c in universe.columns:
            name_col = c
            break

    if name_col is None:
        return {}

    tmp = universe[["code", name_col]].copy()
    tmp["code"] = tmp["code"].astype(str)

    return dict(zip(tmp["code"], tmp[name_col].astype(str)))


# ==============================
# 4. 指定日期股票池构建
# ==============================

def resolve_signal_date(
    requested_date: str,
    bench_df: pd.DataFrame,
    use_prev_trading_day: bool = False,
) -> pd.Timestamp:
    """
    处理信号日期。

    如果 requested_date 是交易日，直接返回。
    如果不是交易日：
        - use_prev_trading_day=True：返回前一个交易日；
        - 否则报错。
    """

    dt = pd.Timestamp(requested_date).normalize()
    trading_dates = bench_df["date"].drop_duplicates().sort_values()

    if dt in set(trading_dates):
        return dt

    if not use_prev_trading_day:
        raise ValueError(
            f"{requested_date} 不是基准交易日。"
            f"如果想自动使用前一个交易日，请加参数 --use-prev-trading-day"
        )

    prev_dates = trading_dates[trading_dates < dt]

    if prev_dates.empty:
        raise ValueError(f"{requested_date} 之前没有可用交易日。")

    resolved = prev_dates.iloc[-1]

    print(f"输入日期 {requested_date} 不是交易日，已自动使用前一个交易日：{resolved.date()}")

    return resolved


def build_pool_on_date(
    signal_date: pd.Timestamp,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    code_name_map: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """
    在指定日期对所有股票进行分类。
    """

    if code_name_map is None:
        code_name_map = {}

    signal_date = pd.Timestamp(signal_date).normalize()

    bench_slice = bench_df[bench_df["date"] <= signal_date].copy()

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

        df_slice = df[df["date"] <= signal_date]

        if len(df_slice) < MIN_BARS:
            continue

        latest = df_slice.iloc[-1]

        # 要求股票在信号日当天有交易数据，避免停牌股票使用旧K线。
        if pd.Timestamp(latest["date"]).normalize() != signal_date:
            continue

        has_nan = False

        for col in required_indicator_cols:
            if col not in latest.index or pd.isna(latest[col]):
                has_nan = True
                break

        if has_nan:
            continue

        rows.append({
            "code": str(code),
            "name": code_name_map.get(str(code), ""),
            "signal_date": signal_date.strftime("%Y-%m-%d"),

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

    # 横截面排名
    df_pool["ret20_rank_pct"] = df_pool["ret20"].rank(pct=True)
    df_pool["ret60_rank_pct"] = df_pool["ret60"].rank(pct=True)

    # 条件
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
# 5. 未来走势计算
# ==============================

def get_forward_trading_dates(
    signal_date: pd.Timestamp,
    bench_df: pd.DataFrame,
    forward_days: int,
) -> List[pd.Timestamp]:
    """
    获取信号日之后的未来 N 个市场交易日。
    用基准指数交易日作为统一日历。
    """

    signal_date = pd.Timestamp(signal_date).normalize()

    future_dates = bench_df.loc[
        bench_df["date"] > signal_date,
        "date"
    ].head(forward_days).tolist()

    return [pd.Timestamp(d).normalize() for d in future_dates]


def calc_forward_return_path_by_calendar(
    df: pd.DataFrame,
    forward_dates: List[pd.Timestamp],
    buy_price_field: str = BUY_PRICE_FIELD,
) -> Dict:
    """
    根据统一交易日历计算未来收益路径。

    口径：
        buy_date = forward_dates[0]
        buy_price = buy_date 当天盘价
        D1 = buy_date 当天收盘价 / buy_price - 1
        D2 = forward_dates[1] 收盘价 / buy_price - 1
        ...
    """

    if df is None or df.empty:
        return {}

    if not forward_dates:
        return {}

    df = df.copy()
    df = normalize_date_col(df)
    df_map = df.set_index("date", drop=False)

    buy_date = forward_dates[0]

    if buy_date not in df_map.index:
        # 买入日没有交易数据，说明买不进去，跳过。
        return {}

    buy_row = df_map.loc[buy_date]

    if isinstance(buy_row, pd.DataFrame):
        buy_row = buy_row.iloc[-1]

    buy_price = get_row_price(buy_row, buy_price_field, fallback="close")

    if pd.isna(buy_price) or buy_price <= 0:
        return {}

    result = {
        "buy_date": buy_date.strftime("%Y-%m-%d"),
        "buy_price": buy_price,
    }

    returns = []

    for i, d in enumerate(forward_dates, start=1):
        if d not in df_map.index:
            ret = np.nan
        else:
            row = df_map.loc[d]

            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]

            close_price = get_row_price(row, "close", fallback="close")

            if pd.isna(close_price) or close_price <= 0:
                ret = np.nan
            else:
                ret = close_price / buy_price - 1

        result[f"D{i}"] = ret
        returns.append(ret)

    ret_series = pd.Series(returns).dropna()

    n = len(forward_dates)

    if len(ret_series) == 0:
        result[f"max_ret_{n}"] = np.nan
        result[f"min_ret_{n}"] = np.nan
        result[f"max_dd_{n}"] = np.nan
        result["day_to_max_ret"] = np.nan
        result["first_profit_5_day"] = np.nan
        result["first_profit_10_day"] = np.nan
        result["first_drawdown_5_day"] = np.nan
        return result

    result[f"max_ret_{n}"] = ret_series.max()
    result[f"min_ret_{n}"] = ret_series.min()

    nav = 1 + ret_series
    result[f"max_dd_{n}"] = max_drawdown(nav)

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


def build_excess_detail(
    detail_df: pd.DataFrame,
    forward_days: int,
) -> pd.DataFrame:
    """
    构建每只股票相对沪深300的超额收益明细。
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
            stock_ret = row.get(col, np.nan)
            bench_ret = bench_row.get(col, np.nan)

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


def build_forward_summary(
    detail_df: pd.DataFrame,
    forward_days: int,
    group_col: str = "status",
) -> pd.DataFrame:
    """
    按状态分组统计未来收益。
    """

    if detail_df is None or detail_df.empty:
        return pd.DataFrame()

    bench_rows = detail_df[detail_df["row_type"] == "benchmark"]
    stock_rows = detail_df[detail_df["row_type"] == "stock"].copy()

    if bench_rows.empty or stock_rows.empty:
        return pd.DataFrame()

    bench_row = bench_rows.iloc[0]

    rows = []

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

            rows.append({
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

    return pd.DataFrame(rows)


def build_overall_forward_summary(
    detail_df: pd.DataFrame,
    forward_days: int,
) -> pd.DataFrame:
    """
    不分组整体统计。
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


# ==============================
# 6. 主分析函数
# ==============================

def analyze_pool_forward_returns(
    signal_date: pd.Timestamp,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    code_name_map: Dict[str, str],
    forward_days: int,
    statuses: List[str],
) -> None:
    """
    指定日期分析股票池未来走势。
    """

    signal_date = pd.Timestamp(signal_date).normalize()

    print("\n========== 指定日期股票池未来走势分析 ==========")
    print(f"信号日期: {signal_date.date()}")
    print(f"分析池: {statuses}")
    print(f"未来交易日数: {forward_days}")
    print("==============================================")

    # 1. 构建当日全市场股票池
    pool_df = build_pool_on_date(
        signal_date=signal_date,
        stock_data=stock_data,
        bench_df=bench_df,
        code_name_map=code_name_map,
    )

    if pool_df.empty:
        print("指定日期未能构建股票池。可能原因：")
        print("1. 该日期不是交易日；")
        print("2. 数据不足；")
        print("3. 当天股票没有交易数据；")
        print("4. 指标窗口不足。")
        return

    # 2. 找出目标池
    selected_df = pool_df[pool_df["status"].isin(statuses)].copy()
    selected_df = selected_df.sort_values("score", ascending=False).reset_index(drop=True)

    if selected_df.empty:
        print(f"{signal_date.date()} 没有股票进入 {statuses}。")

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        output_all_pool = os.path.join(
            OUTPUT_DIR,
            f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_date.strftime('%Y-%m-%d')}_all_pool.csv",
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
        "amount_ratio_5_20",
    ]

    preview_cols = [c for c in preview_cols if c in selected_df.columns]

    print("\n入选股票预览：")
    print(selected_df[preview_cols].head(20).to_string(index=False))

    # 3. 统一未来交易日历
    forward_dates = get_forward_trading_dates(
        signal_date=signal_date,
        bench_df=bench_df,
        forward_days=forward_days,
    )

    if len(forward_dates) < forward_days:
        print(f"基准未来交易日不足：需要 {forward_days} 天，实际只有 {len(forward_dates)} 天。")
        print("请降低 --days，或者选择更早的信号日期。")
        return

    date_map_df = pd.DataFrame({
        "horizon": list(range(1, forward_days + 1)),
        "date": [d.strftime("%Y-%m-%d") for d in forward_dates],
    })

    # 4. 计算基准未来走势
    bench_path = calc_forward_return_path_by_calendar(
        df=bench_df,
        forward_dates=forward_dates,
        buy_price_field=BUY_PRICE_FIELD,
    )

    if not bench_path:
        print("基准未来走势计算失败。")
        return

    detail_rows = []

    bench_row = {
        "row_type": "benchmark",
        "code": "沪深300",
        "name": "沪深300",
        "status": "benchmark",
        "score": np.nan,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
    }

    bench_row.update(bench_path)
    detail_rows.append(bench_row)

    # 5. 计算每只股票未来走势
    skipped = 0

    for _, stock in selected_df.iterrows():
        code = str(stock["code"])

        if code not in stock_data:
            skipped += 1
            continue

        path = calc_forward_return_path_by_calendar(
            df=stock_data[code],
            forward_dates=forward_dates,
            buy_price_field=BUY_PRICE_FIELD,
        )

        if not path:
            skipped += 1
            continue

        row = {
            "row_type": "stock",
            "code": code,
            "name": stock.get("name", code_name_map.get(code, "")),
            "status": stock["status"],
            "score": stock["score"],
            "signal_date": signal_date.strftime("%Y-%m-%d"),

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
        print(f"有 {skipped} 只股票由于买入日无交易、价格异常或未来数据缺失被跳过。")

    # 6. 生成统计表
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

    # 7. 保存 Excel
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_xlsx = os.path.join(
        OUTPUT_DIR,
        f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_date.strftime('%Y-%m-%d')}.xlsx",
    )

    output_detail_csv = os.path.join(
        OUTPUT_DIR,
        f"{FORWARD_ANALYSIS_OUTPUT_PREFIX}_{signal_date.strftime('%Y-%m-%d')}_detail.csv",
    )

    try:
        with pd.ExcelWriter(output_xlsx) as writer:
            detail_df.to_excel(writer, sheet_name="走势明细", index=False)
            excess_df.to_excel(writer, sheet_name="超额收益", index=False)
            group_summary_df.to_excel(writer, sheet_name="分组统计", index=False)
            overall_summary_df.to_excel(writer, sheet_name="整体统计", index=False)
            selected_df.to_excel(writer, sheet_name="入选股票", index=False)
            pool_df.to_excel(writer, sheet_name="全市场分类", index=False)
            date_map_df.to_excel(writer, sheet_name="交易日映射", index=False)

        print(f"\n分析结果已保存到：{output_xlsx}")

    except Exception as e:
        print(f"保存 Excel 失败：{e}")
        print("改为保存 CSV 明细。")

        detail_df.to_csv(output_detail_csv, index=False, encoding="utf-8-sig")
        print(f"走势明细已保存到：{output_detail_csv}")

    # 8. 控制台输出摘要
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
                "win_rate": "{:.2%}".format,
                "bench_ret": "{:.2%}".format,
                "avg_excess": "{:.2%}".format,
                "outperform_rate": "{:.2%}".format,
            }))


# ==============================
# 7. 命令行入口
# ==============================

def parse_args():
    parser = argparse.ArgumentParser(
        description="指定日期股票池未来走势分析"
    )

    parser.add_argument(
        "--date",
        required=True,
        help="信号日期，例如 2026-04-30。必须是交易日；如果不是，可加 --use-prev-trading-day。",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="统计未来多少个交易日，默认 30。",
    )

    parser.add_argument(
        "--statuses",
        type=str,
        default="强趋势池,趋势观察池",
        help="要分析的股票池，逗号分隔，默认：强趋势池,趋势观察池。",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="强制刷新股票和基准数据缓存。",
    )

    parser.add_argument(
        "--use-prev-trading-day",
        action="store_true",
        help="如果输入日期不是交易日，则自动使用前一个交易日。",
    )

    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data",
        help="数据缓存目录，默认 data。",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="输出目录，默认 output。",
    )

    parser.add_argument(
        "--history-days",
        type=int,
        default=2000,
        help="每只股票拉取历史数据天数，默认 2000。",
    )

    parser.add_argument(
        "--min-bars",
        type=int,
        default=130,
        help="最少K线数量，默认 130。",
    )

    parser.add_argument(
        "--min-score",
        type=int,
        default=75,
        help="趋势观察池最低评分，默认 75。",
    )

    parser.add_argument(
        "--min-amount",
        type=float,
        default=80_000_000,
        help="20日平均成交额门槛，默认 80000000。",
    )

    parser.add_argument(
        "--benchmark",
        type=str,
        default="sh000300",
        help="基准指数代码，默认 sh000300，即沪深300。",
    )

    return parser.parse_args()


def main():
    global CACHE_DIR
    global OUTPUT_DIR
    global BENCHMARK_SYMBOL
    global BENCHMARK_CACHE_FILE
    global HISTORY_DAYS
    global FORCE_REFRESH_DATA
    global MIN_BARS
    global MIN_SCORE
    global MIN_AVG_AMOUNT_20

    args = parse_args()

    CACHE_DIR = args.cache_dir
    OUTPUT_DIR = args.output_dir
    BENCHMARK_SYMBOL = args.benchmark
    BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

    HISTORY_DAYS = args.history_days
    FORCE_REFRESH_DATA = bool(args.refresh)

    MIN_BARS = args.min_bars
    MIN_SCORE = args.min_score
    MIN_AVG_AMOUNT_20 = args.min_amount

    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("========== 指定日期股票池未来走势分析 ==========")
    print(f"输入信号日期: {args.date}")
    print(f"未来交易日数: {args.days}")
    print(f"分析池: {statuses}")
    print(f"缓存目录: {CACHE_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"强制刷新数据: {FORCE_REFRESH_DATA}")
    print(f"基准指数: {BENCHMARK_SYMBOL}")
    print("==============================================")

    print("\n获取股票列表...")
    universe = get_mainboard_stocks()

    if universe is None or universe.empty:
        print("股票列表为空，程序终止。")
        return

    if "code" not in universe.columns:
        print("股票列表缺少 code 字段，程序终止。")
        return

    universe = universe.copy()
    universe["code"] = universe["code"].astype(str)

    codes = universe["code"].tolist()
    code_name_map = build_code_name_map(universe)

    print(f"股票数量: {len(codes)}")

    print("\n加载基准数据...")
    bench_df = fetch_benchmark_data(BENCHMARK_SYMBOL)
    bench_df = normalize_date_col(bench_df)

    try:
        signal_date = resolve_signal_date(
            requested_date=args.date,
            bench_df=bench_df,
            use_prev_trading_day=args.use_prev_trading_day,
        )
    except Exception as e:
        print(f"信号日期错误：{e}")
        return

    print("\n预加载股票数据...")
    stock_data = preload_all_stock_data(codes)

    if not stock_data:
        print("没有成功加载任何股票数据，程序终止。")
        return

    analyze_pool_forward_returns(
        signal_date=signal_date,
        stock_data=stock_data,
        bench_df=bench_df,
        code_name_map=code_name_map,
        forward_days=args.days,
        statuses=statuses,
    )


if __name__ == "__main__":
    main()