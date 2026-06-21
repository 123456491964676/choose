# #!/usr/bin/env python
# # -*- coding: utf-8 -*-

# """
# A股趋势策略历史回测脚本 —— 短期数据适配版

# 注意：
# - 适配 ~22 个交易日的短期数据，指标使用 ma5/ma10 等。
# - 回测区间限制在数据实际覆盖范围内。
# - 结果无统计学意义，仅用于验证程序流程。
# """

# import os
# import warnings
# from datetime import datetime, timedelta
# from typing import Optional, Dict, List, Tuple

# import numpy as np
# import pandas as pd
# import akshare as ak
# from tqdm import tqdm
# from concurrent.futures import ThreadPoolExecutor, as_completed

# from stock_data import fetch_stock_history, normalize_code

# warnings.filterwarnings("ignore")

# # ==============================
# # 0. 回测参数（短期适配）
# # ==============================

# BACKTEST_START = "2026-05-25"
# BACKTEST_END   = "2026-06-12"
# HOLD_DAYS      = 3                     # 短期持有
# MIN_SCORE      = 75
# POOL_STATUSES  = ["强趋势池", "趋势观察池"]
# MIN_BARS       = 10                    # 至少20天才能计算 ma20
# MIN_AVG_AMOUNT_20 = 80_000_000

# BENCHMARK_SYMBOL = "sh000300"
# CACHE_DIR = "data"
# OUTPUT_DIR = "output"
# BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

# MAX_WORKERS = 8
# SLEEP_ON_ERROR = 0.5


# # ==============================
# # 1. 指标计算（短期版，与主程序一致）
# # ==============================

# def max_drawdown(series: pd.Series) -> float:
#     s = series.dropna()
#     if len(s) == 0:
#         return np.nan
#     cum_max = s.cummax()
#     dd = s / cum_max - 1
#     return dd.min()


# def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
#     df = df.copy()
#     df["pre_close"] = df["close"].shift(1)

#     df["ma5"] = df["close"].rolling(5).mean()
#     df["ma10"] = df["close"].rolling(10).mean()
#     df["ma20"] = df["close"].rolling(20).mean()

#     df["ma5_slope_2"] = df["ma5"] / df["ma5"].shift(2) - 1
#     df["ma10_slope_3"] = df["ma10"] / df["ma10"].shift(3) - 1

#     df["ret3"] = df["close"] / df["close"].shift(3) - 1
#     df["ret5"] = df["close"] / df["close"].shift(5) - 1
#     df["ret10"] = df["close"] / df["close"].shift(10) - 1
#     df["ret20"] = df["close"] / df["close"].shift(20) - 1

#     df["amount_ma5"] = df["amount"].rolling(5).mean()
#     df["amount_ma20"] = df["amount"].rolling(20).mean()
#     df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

#     if "turnover" in df.columns:
#         df["turnover_ma5"] = df["turnover"].rolling(5).mean()
#         df["turnover_ma10"] = df["turnover"].rolling(10).mean()
#     else:
#         df["turnover_ma5"] = np.nan
#         df["turnover_ma10"] = np.nan

#     df["high5"] = df["high"].rolling(5).max()
#     df["high10"] = df["high"].rolling(10).max()
#     df["high20"] = df["high"].rolling(20).max()
#     df["is_5d_high"] = df["close"] >= df["high5"] * 0.999
#     df["is_10d_high"] = df["close"] >= df["high10"] * 0.999
#     df["dist_to_10d_high"] = df["close"] / df["high10"] - 1

#     df["max_dd10"] = np.nan
#     if len(df) >= 10:
#         for i in range(9, len(df)):
#             window = df["close"].iloc[i - 9:i + 1]
#             df.loc[df.index[i], "max_dd10"] = max_drawdown(window)

#     df["is_up_day"] = df["close"] > df["pre_close"]
#     df["is_down_day"] = df["close"] < df["pre_close"]

#     up_down_ratio_list = []
#     for i in range(len(df)):
#         if i < 9:
#             up_down_ratio_list.append(np.nan)
#             continue
#         window = df.iloc[i - 9:i + 1]
#         up_amount = window.loc[window["is_up_day"], "amount"].sum()
#         down_amount = window.loc[window["is_down_day"], "amount"].sum()
#         if down_amount <= 0:
#             up_down_ratio = np.nan
#         else:
#             up_down_ratio = up_amount / down_amount
#         up_down_ratio_list.append(up_down_ratio)
#     df["up_down_amount_ratio10"] = up_down_ratio_list

#     if "pct_chg" in df.columns:
#         df["is_limit_up"] = df["pct_chg"] >= 9.5
#         df["limit_up_count_3"] = df["is_limit_up"].rolling(3).sum()
#     else:
#         df["limit_up_count_3"] = np.nan

#     if "pct_chg" in df.columns:
#         intraday_range = df["high"] - df["low"]
#         close_position = np.where(
#             intraday_range > 0,
#             (df["close"] - df["low"]) / intraday_range,
#             np.nan,
#         )
#         df["heavy_bearish_candle"] = (
#             (df["pct_chg"] <= -5)
#             & (df["amount"] > df["amount_ma20"] * 1.5)
#             & (close_position <= 0.35)
#         )
#     else:
#         df["heavy_bearish_candle"] = False

#     return df


# def calc_score(row: pd.Series, bench_ret5: float, bench_ret10: float) -> int:
#     score = 0
#     if row["close"] > row["ma5"]: score += 5
#     if row["ma5"] > row["ma10"]: score += 8
#     if row["ma10"] > row["ma20"]: score += 7
#     if row["ma5_slope_2"] > 0: score += 5
#     if row["ma10_slope_3"] > 0: score += 5

#     if row["ret5_rank_pct"] >= 0.70: score += 8
#     if row["ret10_rank_pct"] >= 0.70: score += 8
#     if not pd.isna(bench_ret5):
#         if row["ret5"] > bench_ret5 + 0.02: score += 5
#         elif row["ret5"] > bench_ret5: score += 3
#     if not pd.isna(bench_ret10):
#         if row["ret10"] > bench_ret10 + 0.05: score += 4
#         elif row["ret10"] > bench_ret10: score += 2

#     ratio = row["amount_ratio_5_20"]
#     if 1.2 <= ratio <= 4: score += 6
#     elif 1.0 <= ratio < 1.2: score += 3
#     if row["up_down_amount_ratio10"] > 1.2: score += 4
#     elif row["up_down_amount_ratio10"] > 1.0: score += 2
#     if (row["is_5d_high"] or row["is_10d_high"]) and ratio >= 1.2: score += 3
#     if ratio < 4: score += 2

#     if row["is_5d_high"]: score += 4
#     if row["is_10d_high"]: score += 6
#     if row["dist_to_10d_high"] >= -0.03: score += 3
#     elif row["dist_to_10d_high"] >= -0.05: score += 2
#     if row["close"] > row["ma5"] and row["dist_to_10d_high"] >= -0.05: score += 2

#     max_dd10 = row["max_dd10"]
#     if not pd.isna(max_dd10):
#         if max_dd10 > -0.05: score += 5
#         elif max_dd10 > -0.10: score += 3
#         elif max_dd10 > -0.15: score += 1

#     overheat = False
#     if not pd.isna(row["ret3"]) and row["ret3"] > 0.15: overheat = True
#     if not pd.isna(row["ret5"]) and row["ret5"] > 0.25: overheat = True
#     if not pd.isna(row["limit_up_count_3"]) and row["limit_up_count_3"] >= 2: overheat = True
#     if not overheat: score += 4
#     if not row["heavy_bearish_candle"]: score += 3

#     turnover_ma5 = row["turnover_ma5"]
#     if pd.isna(turnover_ma5): score += 2
#     else:
#         if turnover_ma5 < 15: score += 3
#         elif turnover_ma5 < 25: score += 1

#     return int(min(score, 100))


# def is_overheat(row: pd.Series) -> bool:
#     if not pd.isna(row["ret3"]) and row["ret3"] > 0.15: return True
#     if not pd.isna(row["ret5"]) and row["ret5"] > 0.25: return True
#     if not pd.isna(row["limit_up_count_3"]) and row["limit_up_count_3"] >= 2: return True
#     return False


# def classify_status(row: pd.Series) -> str:
#     if not row["basic_liquid"]: return "流动性不足"
#     if row["close"] < row["ma10"]: return "跌破MA10剔除"
#     if row["close"] < row["ma5"]: return "跌破MA5观察"
#     if row["heavy_bearish_candle"]: return "放量长阴观察"
#     if is_overheat(row): return "短期过热"
#     if row["score"] >= 85 and row["candidate"]: return "强趋势池"
#     if row["score"] >= MIN_SCORE and row["candidate"]: return "趋势观察池"
#     if (row["close"] > row["ma10"] and row["ma5"] > row["ma10"]
#             and abs(row["close"] / row["ma5"] - 1) <= 0.05
#             and row["ret10_rank_pct"] >= 0.60):
#         return "回踩观察"
#     return "剔除"


# # ==============================
# # 2. 股票列表与基准
# # ==============================

# def get_mainboard_stocks() -> pd.DataFrame:
#     from stock_data import get_mainboard_stocks as _get
#     return _get()


# def fetch_benchmark_data(symbol: str = BENCHMARK_SYMBOL) -> pd.DataFrame:
#     if os.path.exists(BENCHMARK_CACHE_FILE):
#         try:
#             df = pd.read_csv(BENCHMARK_CACHE_FILE, parse_dates=["date"])
#             if not df.empty:
#                 return df
#         except Exception:
#             pass
#     try:
#         df = ak.stock_zh_index_daily(symbol=symbol)
#         if "date" not in df.columns and "日期" in df.columns:
#             df = df.rename(columns={"日期": "date"})
#         df["date"] = pd.to_datetime(df["date"])
#         df = df.sort_values("date").reset_index(drop=True)
#         df.to_csv(BENCHMARK_CACHE_FILE, index=False)
#         return df
#     except Exception as e:
#         print(f"获取基准数据失败：{e}")
#         raise


# def get_trading_days(bench_df: pd.DataFrame, start: str, end: str) -> List[datetime]:
#     mask = (bench_df["date"] >= pd.Timestamp(start)) & (bench_df["date"] <= pd.Timestamp(end))
#     return bench_df.loc[mask, "date"].tolist()


# # ==============================
# # 3. 预加载股票数据
# # ==============================

# def preload_all_stock_data(codes: List[str]) -> Dict[str, pd.DataFrame]:
#     data = {}
#     print("正在预加载股票历史数据...")
#     for code in tqdm(codes):
#         df = fetch_stock_history(
#             code,
#             history_days=2000,
#             data_dir=CACHE_DIR,
#             cache_valid_hours=99999,
#             adjust="qfq",
#             min_bars=MIN_BARS,
#             sleep_on_error=0.1,
#         )
#         if df is not None and not df.empty:
#             data[code] = df
#     print(f"成功加载 {len(data)} 只股票数据。")
#     return data


# # ==============================
# # 4. 单日回测
# # ==============================

# def backtest_on_date(
#     date: datetime,
#     stock_data: Dict[str, pd.DataFrame],
#     bench_df: pd.DataFrame,
# ) -> List[Dict]:
#     results = []

#     bench_slice = bench_df[bench_df["date"] <= date]
#     if len(bench_slice) < 11:    # 至少需要10日前收盘价
#         return results

#     bench_close_date = bench_slice.iloc[-1]["close"]
#     bench_close_5 = bench_slice.iloc[-6]["close"] if len(bench_slice) >= 6 else np.nan
#     bench_close_10 = bench_slice.iloc[-11]["close"] if len(bench_slice) >= 11 else np.nan
#     bench_ret5 = bench_close_date / bench_close_5 - 1 if not pd.isna(bench_close_5) else np.nan
#     bench_ret10 = bench_close_date / bench_close_10 - 1 if not pd.isna(bench_close_10) else np.nan

#     future_bench_row = bench_df[bench_df["date"] > date].head(HOLD_DAYS)
#     if len(future_bench_row) < HOLD_DAYS:
#         return results
#     future_bench_close = future_bench_row.iloc[-1]["close"]
#     bench_hold_ret = future_bench_close / bench_close_date - 1

#     rows = []
#     for code, df in stock_data.items():
#         df_slice = df[df["date"] <= date].copy()
#         if len(df_slice) < MIN_BARS:
#             continue
#         df_ind = add_indicators(df_slice)
#         latest = df_ind.iloc[-1]

#         # 核心字段检查（短期版）
#         if pd.isna(latest["close"]) or pd.isna(latest["ma5"]) or pd.isna(latest["ma10"]) \
#                 or pd.isna(latest["ret5"]) or pd.isna(latest["amount_ma20"]) \
#                 or pd.isna(latest["amount_ratio_5_20"]):
#             continue

#         future_df = df[df["date"] > date].head(HOLD_DAYS)
#         if len(future_df) < HOLD_DAYS:
#             continue
#         buy_price = latest["close"]
#         sell_price = future_df.iloc[-1]["close"]
#         hold_ret = sell_price / buy_price - 1

#         row_dict = {
#             "code": code,
#             "close": latest["close"],
#             "ma5": latest["ma5"],
#             "ma10": latest["ma10"],
#             "ma20": latest["ma20"] if not pd.isna(latest["ma20"]) else np.nan,
#             "ma5_slope_2": latest["ma5_slope_2"],
#             "ma10_slope_3": latest["ma10_slope_3"],
#             "ret3": latest["ret3"],
#             "ret5": latest["ret5"],
#             "ret10": latest["ret10"],
#             "amount_ma5": latest["amount_ma5"],
#             "amount_ma20": latest["amount_ma20"],
#             "amount_ratio_5_20": latest["amount_ratio_5_20"],
#             "turnover_ma5": latest.get("turnover_ma5", np.nan),
#             "high5": latest["high5"],
#             "high10": latest["high10"],
#             "is_5d_high": bool(latest["is_5d_high"]),
#             "is_10d_high": bool(latest["is_10d_high"]),
#             "dist_to_10d_high": latest["dist_to_10d_high"],
#             "max_dd10": latest["max_dd10"],
#             "up_down_amount_ratio10": latest["up_down_amount_ratio10"],
#             "limit_up_count_3": latest.get("limit_up_count_3", np.nan),
#             "heavy_bearish_candle": bool(latest.get("heavy_bearish_candle", False)),
#             "buy_price": buy_price,
#             "sell_price": sell_price,
#             "hold_ret": hold_ret,
#             "bench_ret5": bench_ret5,
#             "bench_ret10": bench_ret10,
#             "bench_hold_ret": bench_hold_ret,
#         }
#         rows.append(row_dict)

#     if not rows:
#         return results

#     df_pool = pd.DataFrame(rows)
#     df_pool["ret5_rank_pct"] = df_pool["ret5"].rank(pct=True)
#     df_pool["ret10_rank_pct"] = df_pool["ret10"].rank(pct=True)

#     df_pool["basic_liquid"] = df_pool["amount_ma20"] >= MIN_AVG_AMOUNT_20

#     df_pool["trend_basic"] = (
#         (df_pool["close"] > df_pool["ma5"])
#         & (df_pool["ma5"] > df_pool["ma10"])
#         & (df_pool["ma10"] > df_pool["ma20"])
#         & (df_pool["ma5_slope_2"] > 0)
#         & (df_pool["ma10_slope_3"] > 0)
#     )

#     df_pool["relative_strength"] = (
#         (df_pool["ret5_rank_pct"] >= 0.70) & (df_pool["ret10_rank_pct"] >= 0.70)
#     )

#     df_pool["volume_ok"] = (
#         (df_pool["amount_ratio_5_20"] >= 1.2) & (df_pool["amount_ratio_5_20"] <= 4)
#     )

#     df_pool["near_breakout"] = (
#         (df_pool["dist_to_10d_high"] >= -0.05) | df_pool["is_5d_high"] | df_pool["is_10d_high"]
#     )

#     df_pool["risk_ok"] = (
#         (df_pool["close"] > df_pool["ma5"])
#         & (df_pool["max_dd10"] > -0.20)
#         & (~df_pool["heavy_bearish_candle"])
#     )

#     df_pool["candidate"] = (
#         df_pool["basic_liquid"] & df_pool["trend_basic"] & df_pool["relative_strength"]
#         & df_pool["volume_ok"] & df_pool["near_breakout"] & df_pool["risk_ok"]
#     )

#     df_pool["score"] = df_pool.apply(lambda row: calc_score(row, bench_ret5, bench_ret10), axis=1)
#     df_pool["status"] = df_pool.apply(classify_status, axis=1)

#     selected = df_pool[df_pool["status"].isin(POOL_STATUSES)]
#     for _, stock in selected.iterrows():
#         results.append({
#             "date": date.strftime("%Y-%m-%d"),
#             "code": stock["code"],
#             "status": stock["status"],
#             "score": stock["score"],
#             "buy_price": stock["buy_price"],
#             "sell_price": stock["sell_price"],
#             "hold_ret": stock["hold_ret"],
#             "bench_hold_ret": bench_hold_ret,
#             "excess_ret": stock["hold_ret"] - bench_hold_ret,
#         })
#     return results


# # ==============================
# # 5. 主回测流程
# # ==============================

# def run_backtest():
#     os.makedirs(OUTPUT_DIR, exist_ok=True)
#     print("获取股票列表...")
#     universe = get_mainboard_stocks()
#     codes = universe["code"].tolist()
#     print(f"共 {len(codes)} 只股票。")

#     print("加载基准数据...")
#     bench_df = fetch_benchmark_data()
#     bench_df = bench_df.sort_values("date").reset_index(drop=True)

#     trading_days = get_trading_days(bench_df, BACKTEST_START, BACKTEST_END)
#     print(f"回测区间交易日数: {len(trading_days)}")

#     print("预加载股票历史数据（使用缓存）...")
#     stock_data = preload_all_stock_data(codes)

#     all_trades = []
#     for date in tqdm(trading_days, desc="回测进度"):
#         try:
#             trades = backtest_on_date(date, stock_data, bench_df)
#             all_trades.extend(trades)
#         except Exception as e:
#             print(f"日期 {date.date()} 回测出错：{e}")

#     if not all_trades:
#         print("回测未产生任何交易。")
#         return

#     trades_df = pd.DataFrame(all_trades)
#     trades_df.to_csv(os.path.join(OUTPUT_DIR, "backtest_trades.csv"), index=False, encoding="utf-8-sig")
#     print(f"共产生 {len(trades_df)} 次交易，已保存到 output/backtest_trades.csv")

#     print("\n========== 回测统计 ==========")
#     avg_ret = trades_df["hold_ret"].mean()
#     win_rate = (trades_df["hold_ret"] > 0).mean()
#     avg_excess = trades_df["excess_ret"].mean()
#     total_return = (1 + trades_df["hold_ret"]).prod() - 1 if len(trades_df) > 0 else 0

#     print(f"交易总次数: {len(trades_df)}")
#     print(f"平均持有期收益: {avg_ret:.4%}")
#     print(f"胜率: {win_rate:.2%}")
#     print(f"平均超额收益 (相对沪深300): {avg_excess:.4%}")
#     print(f"总收益（按交易序列连乘）: {total_return:.4%}")

#     if len(trades_df) > 1:
#         cum_ret = (1 + trades_df["hold_ret"]).cumprod()
#         max_dd = (cum_ret / cum_ret.cummax() - 1).min()
#         print(f"交易序列最大回撤: {max_dd:.4%}")

#     trades_df["year"] = pd.to_datetime(trades_df["date"]).dt.year
#     yearly = trades_df.groupby("year").agg(
#         交易次数=("code", "count"),
#         平均收益=("hold_ret", "mean"),
#         胜率=("hold_ret", lambda x: (x > 0).mean()),
#         平均超额=("excess_ret", "mean"),
#     )
#     print("\n年度统计:")
#     print(yearly.to_string())


# if __name__ == "__main__":
#     run_backtest()

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A股趋势策略历史回测脚本 —— 极简短期版（强行降低门槛）

注意：
- 本脚本已将几乎所有数据门槛降到极低，**仅用于验证代码流程**。
- 结果无任何统计学意义，切勿用于实盘决策。
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
# 0. 回测参数（极低门槛）
# ==============================
BACKTEST_START = "2026-05-29"      # 推迟至基准至少有5日数据
BACKTEST_END   = "2026-06-12"
HOLD_DAYS      = 3
MIN_SCORE      = 30                # 降低最低分
POOL_STATUSES  = ["强趋势池", "趋势观察池", "回踩观察", "短期过热"]   # 放宽池
MIN_BARS       = 3                 # 极小数据量要求
MIN_AVG_AMOUNT_20 = 0              # 关闭流动性门槛（短期数据无法计算）

BENCHMARK_SYMBOL = "sh000300"
CACHE_DIR = "data"
OUTPUT_DIR = "output"
BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

MAX_WORKERS = 1                    # 单线程避免缓存并发问题
SLEEP_ON_ERROR = 0.5

# ==============================
# 1. 指标计算（极简版）
# ==============================
def max_drawdown(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) == 0:
        return np.nan
    cum_max = s.cummax()
    dd = s / cum_max - 1
    return dd.min()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pre_close"] = df["close"].shift(1)

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    # 不再依赖 ma20

    df["ma5_slope_2"] = df["ma5"] / df["ma5"].shift(2) - 1
    df["ma10_slope_3"] = df["ma10"] / df["ma10"].shift(3) - 1

    df["ret3"] = df["close"] / df["close"].shift(3) - 1
    df["ret5"] = df["close"] / df["close"].shift(5) - 1
    df["ret10"] = df["close"] / df["close"].shift(10) - 1

    df["amount_ma5"] = df["amount"].rolling(5).mean()
    # 不需要 amount_ma20

    if "turnover" in df.columns:
        df["turnover_ma5"] = df["turnover"].rolling(5).mean()
    else:
        df["turnover_ma5"] = np.nan

    df["high5"] = df["high"].rolling(5).max()
    df["high10"] = df["high"].rolling(10).max()
    df["is_5d_high"] = df["close"] >= df["high5"] * 0.999
    df["is_10d_high"] = df["close"] >= df["high10"] * 0.999
    df["dist_to_10d_high"] = df["close"] / df["high10"] - 1

    df["max_dd10"] = np.nan
    if len(df) >= 10:
        for i in range(9, len(df)):
            window = df["close"].iloc[i - 9:i + 1]
            df.loc[df.index[i], "max_dd10"] = max_drawdown(window)

    df["is_up_day"] = df["close"] > df["pre_close"]
    df["is_down_day"] = df["close"] < df["pre_close"]

    # up_down_amount_ratio 需要 10 天
    up_down_ratio_list = []
    for i in range(len(df)):
        if i < 9:
            up_down_ratio_list.append(np.nan)
            continue
        window = df.iloc[i - 9:i + 1]
        up_amount = window.loc[window["is_up_day"], "amount"].sum()
        down_amount = window.loc[window["is_down_day"], "amount"].sum()
        if down_amount <= 0:
            up_down_ratio = np.nan
        else:
            up_down_ratio = up_amount / down_amount
        up_down_ratio_list.append(up_down_ratio)
    df["up_down_amount_ratio10"] = up_down_ratio_list

    if "pct_chg" in df.columns:
        df["is_limit_up"] = df["pct_chg"] >= 9.5
        df["limit_up_count_3"] = df["is_limit_up"].rolling(3).sum()
    else:
        df["limit_up_count_3"] = np.nan

    # 放量长阴因为需要 amount_ma20，直接设为 False
    df["heavy_bearish_candle"] = False

    return df

def calc_score(row: pd.Series, bench_ret5: float, bench_ret10: float) -> int:
    score = 0
    # 均线
    if row["close"] > row["ma5"]: score += 5
    if row["ma5"] > row["ma10"]: score += 8
    # 不再检查 ma20

    if row["ma5_slope_2"] > 0: score += 5
    if row["ma10_slope_3"] > 0: score += 5

    # 相对强度（简化）
    if row["ret5_rank_pct"] >= 0.70: score += 8
    if not pd.isna(row.get("ret10_rank_pct")) and row["ret10_rank_pct"] >= 0.70: score += 8

    if not pd.isna(bench_ret5):
        if row["ret5"] > bench_ret5 + 0.02: score += 5
        elif row["ret5"] > bench_ret5: score += 3

    # 量价（用 amount_ma5 简单替代）
    if row.get("turnover_ma5") and not pd.isna(row["turnover_ma5"]) and row["turnover_ma5"] < 25: score += 3

    # 突破
    if row["is_5d_high"]: score += 4
    if row["is_10d_high"]: score += 6
    if not pd.isna(row["dist_to_10d_high"]) and row["dist_to_10d_high"] >= -0.05: score += 2

    # 回撤
    if not pd.isna(row["max_dd10"]) and row["max_dd10"] > -0.10: score += 3

    # 无过热加分
    score += 4  # 简单给满

    return int(min(score, 100))

def classify_status(row: pd.Series) -> str:
    # 极度简化的状态分类
    if row["close"] < row["ma5"]: return "跌破MA5观察"
    if row["score"] >= 85: return "强趋势池"
    if row["score"] >= MIN_SCORE: return "趋势观察池"
    return "回踩观察"

# ==============================
# 2. 股票列表与基准
# ==============================
def get_mainboard_stocks() -> pd.DataFrame:
    from stock_data import get_mainboard_stocks as _get
    return _get()

def fetch_benchmark_data(symbol: str = BENCHMARK_SYMBOL) -> pd.DataFrame:
    if os.path.exists(BENCHMARK_CACHE_FILE):
        try:
            df = pd.read_csv(BENCHMARK_CACHE_FILE, parse_dates=["date"])
            if not df.empty:
                return df
        except Exception:
            pass
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if "date" not in df.columns and "日期" in df.columns:
            df = df.rename(columns={"日期": "date"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df.to_csv(BENCHMARK_CACHE_FILE, index=False)
        return df
    except Exception as e:
        print(f"获取基准数据失败：{e}")
        raise

def get_trading_days(bench_df: pd.DataFrame, start: str, end: str) -> List[datetime]:
    mask = (bench_df["date"] >= pd.Timestamp(start)) & (bench_df["date"] <= pd.Timestamp(end))
    return bench_df.loc[mask, "date"].tolist()

# ==============================
# 3. 预加载股票数据
# ==============================
def preload_all_stock_data(codes: List[str]) -> Dict[str, pd.DataFrame]:
    data = {}
    print("正在预加载股票历史数据...")
    for code in tqdm(codes):
        df = fetch_stock_history(
            code,
            history_days=2000,
            data_dir=CACHE_DIR,
            cache_valid_hours=99999,
            adjust="qfq",
            min_bars=MIN_BARS,        # 极小值
            sleep_on_error=0.1,
        )
        if df is not None and not df.empty:
            data[code] = df
    print(f"成功加载 {len(data)} 只股票数据。")
    return data

# ==============================
# 4. 单日回测
# ==============================
def backtest_on_date(
    date: datetime,
    stock_data: Dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
) -> List[Dict]:
    results = []

    # 基准只要足够计算 ret5 即可
    bench_slice = bench_df[bench_df["date"] <= date]
    if len(bench_slice) < 6:      # 只需6日数据算 ret5
        return results

    bench_close_date = bench_slice.iloc[-1]["close"]
    bench_close_5 = bench_slice.iloc[-6]["close"]   # 5个交易日涨幅
    bench_ret5 = bench_close_date / bench_close_5 - 1 if not pd.isna(bench_close_5) else np.nan
    bench_ret10 = np.nan

    future_bench_row = bench_df[bench_df["date"] > date].head(HOLD_DAYS)
    if len(future_bench_row) < HOLD_DAYS:
        return results
    future_bench_close = future_bench_row.iloc[-1]["close"]
    bench_hold_ret = future_bench_close / bench_close_date - 1

    rows = []
    for code, df in stock_data.items():
        df_slice = df[df["date"] <= date].copy()
        if len(df_slice) < MIN_BARS:
            continue
        df_ind = add_indicators(df_slice)
        latest = df_ind.iloc[-1]

        # 只要求 close, ma5, ma10, ret5 有效
        if pd.isna(latest["close"]) or pd.isna(latest["ma5"]) or pd.isna(latest["ma10"]) \
                or pd.isna(latest["ret5"]):
            continue

        # 未来价格
        future_df = df[df["date"] > date].head(HOLD_DAYS)
        if len(future_df) < HOLD_DAYS:
            continue
        buy_price = latest["close"]
        sell_price = future_df.iloc[-1]["close"]
        hold_ret = sell_price / buy_price - 1

        row_dict = {
            "code": code,
            "close": latest["close"],
            "ma5": latest["ma5"],
            "ma10": latest["ma10"],
            "ma5_slope_2": latest["ma5_slope_2"] if not pd.isna(latest["ma5_slope_2"]) else 0,
            "ma10_slope_3": latest["ma10_slope_3"] if not pd.isna(latest["ma10_slope_3"]) else 0,
            "ret3": latest["ret3"] if not pd.isna(latest["ret3"]) else 0,
            "ret5": latest["ret5"],
            "ret10": latest["ret10"] if not pd.isna(latest["ret10"]) else 0,
            "amount_ma5": latest["amount_ma5"] if not pd.isna(latest["amount_ma5"]) else 0,
            "turnover_ma5": latest.get("turnover_ma5", np.nan),
            "high5": latest["high5"] if not pd.isna(latest["high5"]) else latest["close"],
            "high10": latest["high10"] if not pd.isna(latest["high10"]) else latest["close"],
            "is_5d_high": bool(latest["is_5d_high"]) if "is_5d_high" in latest and not pd.isna(latest["is_5d_high"]) else False,
            "is_10d_high": bool(latest["is_10d_high"]) if "is_10d_high" in latest and not pd.isna(latest["is_10d_high"]) else False,
            "dist_to_10d_high": latest["dist_to_10d_high"] if not pd.isna(latest["dist_to_10d_high"]) else 0,
            "max_dd10": latest["max_dd10"] if not pd.isna(latest["max_dd10"]) else 0,
            "up_down_amount_ratio10": latest["up_down_amount_ratio10"] if not pd.isna(latest["up_down_amount_ratio10"]) else 1,
            "limit_up_count_3": latest.get("limit_up_count_3", 0),
            "heavy_bearish_candle": False,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "hold_ret": hold_ret,
            "bench_ret5": bench_ret5,
            "bench_ret10": bench_ret10,
            "bench_hold_ret": bench_hold_ret,
        }
        rows.append(row_dict)

    if not rows:
        return results

    df_pool = pd.DataFrame(rows)
    df_pool["ret5_rank_pct"] = df_pool["ret5"].rank(pct=True)
    df_pool["ret10_rank_pct"] = df_pool["ret10"].rank(pct=True) if "ret10" in df_pool else 0.5

    # 直接认定全部 candidate=True
    df_pool["candidate"] = True

    df_pool["score"] = df_pool.apply(lambda row: calc_score(row, bench_ret5, bench_ret10), axis=1)
    df_pool["status"] = df_pool.apply(classify_status, axis=1)

    # 所有股票都纳入池（因为已经极简）
    for _, stock in df_pool.iterrows():
        results.append({
            "date": date.strftime("%Y-%m-%d"),
            "code": stock["code"],
            "status": stock["status"],
            "score": stock["score"],
            "buy_price": stock["buy_price"],
            "sell_price": stock["sell_price"],
            "hold_ret": stock["hold_ret"],
            "bench_hold_ret": bench_hold_ret,
            "excess_ret": stock["hold_ret"] - bench_hold_ret,
        })
    return results

# ==============================
# 5. 主回测流程
# ==============================
def run_backtest():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("获取股票列表...")
    universe = get_mainboard_stocks()
    codes = universe["code"].tolist()
    print(f"共 {len(codes)} 只股票。")

    print("加载基准数据...")
    bench_df = fetch_benchmark_data()
    bench_df = bench_df.sort_values("date").reset_index(drop=True)

    trading_days = get_trading_days(bench_df, BACKTEST_START, BACKTEST_END)
    print(f"回测区间交易日数: {len(trading_days)}")

    print("预加载股票历史数据（使用缓存）...")
    stock_data = preload_all_stock_data(codes)

    all_trades = []
    for date in tqdm(trading_days, desc="回测进度"):
        try:
            trades = backtest_on_date(date, stock_data, bench_df)
            all_trades.extend(trades)
        except Exception as e:
            print(f"日期 {date.date()} 回测出错：{e}")

    if not all_trades:
        print("回测未产生任何交易。")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df.to_csv(os.path.join(OUTPUT_DIR, "backtest_trades.csv"), index=False, encoding="utf-8-sig")
    print(f"共产生 {len(trades_df)} 次交易，已保存到 output/backtest_trades.csv")

    print("\n========== 回测统计 ==========")
    avg_ret = trades_df["hold_ret"].mean()
    win_rate = (trades_df["hold_ret"] > 0).mean()
    avg_excess = trades_df["excess_ret"].mean()

    print(f"交易总次数: {len(trades_df)}")
    print(f"平均持有期收益: {avg_ret:.4%}")
    print(f"胜率: {win_rate:.2%}")
    print(f"平均超额收益 (相对沪深300): {avg_excess:.4%}")

if __name__ == "__main__":
    run_backtest()