# #!/usr/bin/env python
# # -*- coding: utf-8 -*-

# """
# A股趋势策略历史回测脚本

# 功能：
# - 基于 trend_stock_pool_dp_xl_03.py 中的趋势选股逻辑进行历史回测。
# - 对每个交易日，用“当时”已有的数据选股，模拟持有 N 天后的真实收益。
# - 对比基准沪深300同期收益，统计胜率、平均收益、超额收益等。

# 用法：
#     python backtest.py
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
# # 0. 回测参数
# # ==============================

# BACKTEST_START = "2024-01-01"      # 回测开始日期（包含）
# BACKTEST_END = "2025-12-31"        # 回测结束日期（包含）
# HOLD_DAYS = 20                     # 持有交易日天数
# MIN_SCORE = 75                     # 最低入选分数（参考原策略 min_pool_score）
# POOL_STATUSES = ["强趋势池", "趋势观察池"]   # 哪些状态的股票纳入回测
# MIN_BARS =  10                     # 最少需要的历史交易日数
# MIN_AVG_AMOUNT_20 = 80_000_000     # 流动性门槛（原策略）

# # 基准参数
# BENCHMARK_SYMBOL = "sh000300"      # 沪深300
# CACHE_DIR = "data"                 # 股票数据缓存目录
# OUTPUT_DIR = "output"
# BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

# # 其他配置
# MAX_WORKERS = 8
# SLEEP_ON_ERROR = 0.5

# # ==============================
# # 1. 指标计算函数（从主程序复制，去除全局依赖）
# # ==============================

# def max_drawdown(series: pd.Series) -> float:
#     s = series.dropna()
#     if len(s) == 0:
#         return np.nan
#     cum_max = s.cummax()
#     dd = s / cum_max - 1
#     return dd.min()


# def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
#     """添加趋势指标（同主程序）"""
#     df = df.copy()
#     df["pre_close"] = df["close"].shift(1)

#     df["ma20"] = df["close"].rolling(20).mean()
#     df["ma60"] = df["close"].rolling(60).mean()
#     df["ma120"] = df["close"].rolling(120).mean()

#     df["ma20_slope_5"] = df["ma20"] / df["ma20"].shift(5) - 1
#     df["ma60_slope_10"] = df["ma60"] / df["ma60"].shift(10) - 1

#     df["ret10"] = df["close"] / df["close"].shift(10) - 1
#     df["ret20"] = df["close"] / df["close"].shift(20) - 1
#     df["ret60"] = df["close"] / df["close"].shift(60) - 1

#     df["amount_ma5"] = df["amount"].rolling(5).mean()
#     df["amount_ma20"] = df["amount"].rolling(20).mean()
#     df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

#     if "turnover" in df.columns:
#         df["turnover_ma5"] = df["turnover"].rolling(5).mean()
#         df["turnover_ma20"] = df["turnover"].rolling(20).mean()
#     else:
#         df["turnover_ma5"] = np.nan
#         df["turnover_ma20"] = np.nan

#     df["high20"] = df["high"].rolling(20).max()
#     df["high60"] = df["high"].rolling(60).max()
#     df["is_20d_high"] = df["close"] >= df["high20"] * 0.999
#     df["is_60d_high"] = df["close"] >= df["high60"] * 0.999
#     df["dist_to_60d_high"] = df["close"] / df["high60"] - 1

#     df["max_dd20"] = np.nan
#     if len(df) >= 20:
#         for i in range(19, len(df)):
#             window = df["close"].iloc[i - 19:i + 1]
#             df.loc[df.index[i], "max_dd20"] = max_drawdown(window)

#     df["is_up_day"] = df["close"] > df["pre_close"]
#     df["is_down_day"] = df["close"] < df["pre_close"]

#     up_down_ratio_list = []
#     for i in range(len(df)):
#         if i < 19:
#             up_down_ratio_list.append(np.nan)
#             continue
#         window = df.iloc[i - 19:i + 1]
#         up_amount = window.loc[window["is_up_day"], "amount"].sum()
#         down_amount = window.loc[window["is_down_day"], "amount"].sum()
#         if down_amount <= 0:
#             up_down_ratio = np.nan
#         else:
#             up_down_ratio = up_amount / down_amount
#         up_down_ratio_list.append(up_down_ratio)
#     df["up_down_amount_ratio20"] = up_down_ratio_list

#     if "pct_chg" in df.columns:
#         df["is_limit_up"] = df["pct_chg"] >= 9.5
#         df["limit_up_count_5"] = df["is_limit_up"].rolling(5).sum()
#     else:
#         df["limit_up_count_5"] = np.nan

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


# def calc_score(row: pd.Series, bench_ret20: float, bench_ret60: float) -> int:
#     """趋势打分（同主程序）"""
#     score = 0
#     # 均线趋势
#     if row["close"] > row["ma20"]: score += 5
#     if row["ma20"] > row["ma60"]: score += 8
#     if row["ma60"] > row["ma120"]: score += 7
#     if row["ma20_slope_5"] > 0: score += 5
#     if row["ma60_slope_10"] > 0: score += 5

#     # 相对强度
#     if row["ret20_rank_pct"] >= 0.70: score += 8
#     if row["ret60_rank_pct"] >= 0.70: score += 8
#     if not pd.isna(bench_ret20):
#         if row["ret20"] > bench_ret20 + 0.05: score += 5
#         elif row["ret20"] > bench_ret20: score += 3
#     if not pd.isna(bench_ret60):
#         if row["ret60"] > bench_ret60 + 0.10: score += 4
#         elif row["ret60"] > bench_ret60: score += 2

#     # 量价配合
#     ratio = row["amount_ratio_5_20"]
#     if 1.2 <= ratio <= 4: score += 6
#     elif 1.0 <= ratio < 1.2: score += 3
#     if row["up_down_amount_ratio20"] > 1.2: score += 4
#     elif row["up_down_amount_ratio20"] > 1.0: score += 2
#     if (row["is_20d_high"] or row["is_60d_high"]) and ratio >= 1.2: score += 3
#     if ratio < 4: score += 2

#     # 突破形态
#     if row["is_20d_high"]: score += 4
#     if row["is_60d_high"]: score += 6
#     if row["dist_to_60d_high"] >= -0.03: score += 3
#     elif row["dist_to_60d_high"] >= -0.05: score += 2
#     if row["close"] > row["ma20"] and row["dist_to_60d_high"] >= -0.05: score += 2

#     # 风险控制
#     max_dd20 = row["max_dd20"]
#     if not pd.isna(max_dd20):
#         if max_dd20 > -0.10: score += 5
#         elif max_dd20 > -0.15: score += 3
#         elif max_dd20 > -0.20: score += 1

#     overheat = False
#     if not pd.isna(row["ret10"]) and row["ret10"] > 0.35: overheat = True
#     if not pd.isna(row["ret20"]) and row["ret20"] > 0.60: overheat = True
#     if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3: overheat = True
#     if not overheat: score += 4
#     if not row["heavy_bearish_candle"]: score += 3

#     turnover_ma5 = row["turnover_ma5"]
#     if pd.isna(turnover_ma5): score += 2
#     else:
#         if turnover_ma5 < 15: score += 3
#         elif turnover_ma5 < 25: score += 1

#     return int(min(score, 100))


# def is_overheat(row: pd.Series) -> bool:
#     if not pd.isna(row["ret10"]) and row["ret10"] > 0.35: return True
#     if not pd.isna(row["ret20"]) and row["ret20"] > 0.60: return True
#     if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3: return True
#     return False


# def classify_status(row: pd.Series) -> str:
#     """分类（同主程序）"""
#     if not row["basic_liquid"]: return "流动性不足"
#     if row["close"] < row["ma60"]: return "跌破MA60剔除"
#     if row["close"] < row["ma20"]: return "跌破MA20观察"
#     if row["heavy_bearish_candle"]: return "放量长阴观察"
#     if is_overheat(row): return "短期过热"
#     if row["score"] >= 85 and row["candidate"]: return "强趋势池"
#     if row["score"] >= MIN_SCORE and row["candidate"]: return "趋势观察池"
#     if (row["close"] > row["ma60"] and row["ma20"] > row["ma60"]
#             and abs(row["close"] / row["ma20"] - 1) <= 0.05
#             and row["ret60_rank_pct"] >= 0.60):
#         return "回踩观察"
#     return "剔除"


# # ==============================
# # 2. 获取主板股票列表和基准数据
# # ==============================

# def get_mainboard_stocks() -> pd.DataFrame:
#     """复用 stock_data 中的逻辑"""
#     from stock_data import get_mainboard_stocks as _get
#     return _get()


# def fetch_benchmark_data(symbol: str = BENCHMARK_SYMBOL) -> pd.DataFrame:
#     """获取基准指数日线数据，并缓存"""
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


# # ==============================
# # 3. 获取交易日列表
# # ==============================

# def get_trading_days(bench_df: pd.DataFrame, start: str, end: str) -> List[datetime]:
#     """从基准数据中提取指定区间的交易日列表"""
#     mask = (bench_df["date"] >= pd.Timestamp(start)) & (bench_df["date"] <= pd.Timestamp(end))
#     return bench_df.loc[mask, "date"].tolist()


# # ==============================
# # 4. 预加载所有股票完整历史数据
# # ==============================

# def preload_all_stock_data(codes: List[str]) -> Dict[str, pd.DataFrame]:
#     """从缓存加载所有股票完整历史，缺失的跳过"""
#     data = {}
#     print("正在预加载股票历史数据...")
#     for code in tqdm(codes):
#         df = fetch_stock_history(
#             code,
#             history_days=2000,               # 足够长的天数
#             data_dir=CACHE_DIR,
#             cache_valid_hours=99999,         # 始终使用缓存
#             adjust="qfq",
#             min_bars=MIN_BARS,               # 至少满足最低要求
#             sleep_on_error=0.1,
#         )
#         if df is not None and not df.empty:
#             data[code] = df
#     print(f"成功加载 {len(data)} 只股票数据。")
#     return data


# # ==============================
# # 5. 对指定日期选股并计算未来收益
# # ==============================

# def backtest_on_date(
#     date: datetime,
#     stock_data: Dict[str, pd.DataFrame],
#     bench_df: pd.DataFrame,
# ) -> List[Dict]:
#     """在给定日期执行一次选股，并记录持有期收益"""
#     results = []

#     # 获取基准到当日及持有期后的数据
#     bench_slice = bench_df[bench_df["date"] <= date]
#     if len(bench_slice) < 61:
#         return results  # 基准数据不足，无法计算

#     # 计算基准收益率
#     bench_close_date = bench_slice.iloc[-1]["close"]
#     bench_close_20 = bench_slice.iloc[-21]["close"] if len(bench_slice) >= 21 else np.nan
#     bench_close_60 = bench_slice.iloc[-61]["close"] if len(bench_slice) >= 61 else np.nan
#     bench_ret20 = bench_close_date / bench_close_20 - 1 if not pd.isna(bench_close_20) else np.nan
#     bench_ret60 = bench_close_date / bench_close_60 - 1 if not pd.isna(bench_close_60) else np.nan

#     # 未来基准持有收益（用于超额计算）
#     future_bench_row = bench_df[bench_df["date"] > date].head(HOLD_DAYS)
#     if len(future_bench_row) < HOLD_DAYS:
#         return results  # 未来数据不足，无法计算持有期
#     future_bench_close = future_bench_row.iloc[-1]["close"]
#     bench_hold_ret = future_bench_close / bench_close_date - 1

#     # 预收集所有股票的当日指标
#     rows = []
#     valid_codes = []
#     for code, df in stock_data.items():
#         df_slice = df[df["date"] <= date].copy()
#         if len(df_slice) < MIN_BARS:
#             continue
#         df_ind = add_indicators(df_slice)
#         latest = df_ind.iloc[-1]
#         # 核心字段必须有效
#         if pd.isna(latest["close"]) or pd.isna(latest["ma20"]) or pd.isna(latest["ma60"]) \
#                 or pd.isna(latest["ret20"]) or pd.isna(latest["amount_ma20"]) or pd.isna(latest["amount_ratio_5_20"]):
#             continue
#         # 获取未来价格
#         future_df = df[df["date"] > date].head(HOLD_DAYS)
#         if len(future_df) < HOLD_DAYS:
#             continue
#         buy_price = latest["close"]
#         sell_price = future_df.iloc[-1]["close"]
#         hold_ret = sell_price / buy_price - 1

#         row_dict = {
#             "code": code,
#             "close": latest["close"],
#             "ma20": latest["ma20"],
#             "ma60": latest["ma60"],
#             "ma120": latest["ma120"] if not pd.isna(latest["ma120"]) else np.nan,
#             "ma20_slope_5": latest["ma20_slope_5"],
#             "ma60_slope_10": latest["ma60_slope_10"],
#             "ret10": latest["ret10"],
#             "ret20": latest["ret20"],
#             "ret60": latest["ret60"],
#             "amount_ma5": latest["amount_ma5"],
#             "amount_ma20": latest["amount_ma20"],
#             "amount_ratio_5_20": latest["amount_ratio_5_20"],
#             "turnover_ma5": latest.get("turnover_ma5", np.nan),
#             "turnover_ma20": latest.get("turnover_ma20", np.nan),
#             "high20": latest["high20"],
#             "high60": latest["high60"],
#             "is_20d_high": bool(latest["is_20d_high"]),
#             "is_60d_high": bool(latest["is_60d_high"]),
#             "dist_to_60d_high": latest["dist_to_60d_high"],
#             "max_dd20": latest["max_dd20"],
#             "up_down_amount_ratio20": latest["up_down_amount_ratio20"],
#             "limit_up_count_5": latest.get("limit_up_count_5", np.nan),
#             "heavy_bearish_candle": bool(latest.get("heavy_bearish_candle", False)),
#             "buy_price": buy_price,
#             "sell_price": sell_price,
#             "hold_ret": hold_ret,
#             "bench_ret20": bench_ret20,
#             "bench_ret60": bench_ret60,
#             "bench_hold_ret": bench_hold_ret,
#         }
#         rows.append(row_dict)
#         valid_codes.append(code)

#     if not rows:
#         return results

#     df_pool = pd.DataFrame(rows)

#     # 全市场排名
#     df_pool["ret20_rank_pct"] = df_pool["ret20"].rank(pct=True)
#     df_pool["ret60_rank_pct"] = df_pool["ret60"].rank(pct=True)

#     # 基础流动性
#     df_pool["basic_liquid"] = df_pool["amount_ma20"] >= MIN_AVG_AMOUNT_20

#     # 趋势基础条件
#     df_pool["trend_basic"] = (
#         (df_pool["close"] > df_pool["ma20"])
#         & (df_pool["ma20"] > df_pool["ma60"])
#         & (df_pool["ma60"] > df_pool["ma120"])
#         & (df_pool["ma20_slope_5"] > 0)
#         & (df_pool["ma60_slope_10"] > 0)
#     )

#     # 相对强度
#     df_pool["relative_strength"] = (
#         (df_pool["ret20_rank_pct"] >= 0.70) & (df_pool["ret60_rank_pct"] >= 0.70)
#     )

#     # 成交额配合
#     df_pool["volume_ok"] = (
#         (df_pool["amount_ratio_5_20"] >= 1.2) & (df_pool["amount_ratio_5_20"] <= 4)
#     )

#     # 突破信号
#     df_pool["near_breakout"] = (
#         (df_pool["dist_to_60d_high"] >= -0.05) | df_pool["is_20d_high"] | df_pool["is_60d_high"]
#     )

#     # 风险过滤
#     df_pool["risk_ok"] = (
#         (df_pool["close"] > df_pool["ma20"])
#         & (df_pool["max_dd20"] > -0.20)
#         & (~df_pool["heavy_bearish_candle"])
#     )

#     df_pool["candidate"] = (
#         df_pool["basic_liquid"] & df_pool["trend_basic"] & df_pool["relative_strength"]
#         & df_pool["volume_ok"] & df_pool["near_breakout"] & df_pool["risk_ok"]
#     )

#     # 评分
#     df_pool["score"] = df_pool.apply(
#         lambda row: calc_score(row, bench_ret20, bench_ret60), axis=1
#     )

#     # 分类
#     df_pool["status"] = df_pool.apply(classify_status, axis=1)

#     # 筛选入池股票
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
# # 6. 主回测流程
# # ==============================

# def run_backtest():
#     # 准备工作
#     os.makedirs(OUTPUT_DIR, exist_ok=True)
#     print("获取股票列表...")
#     universe = get_mainboard_stocks()
#     codes = universe["code"].tolist()
#     print(f"共 {len(codes)} 只股票。")

#     print("加载基准数据...")
#     bench_df = fetch_benchmark_data()
#     bench_df = bench_df.sort_values("date").reset_index(drop=True)

#     # 获取交易日
#     trading_days = get_trading_days(bench_df, BACKTEST_START, BACKTEST_END)
#     print(f"回测区间交易日数: {len(trading_days)}")

#     # 预加载股票数据（用多线程加速）
#     print("预加载股票历史数据（使用缓存）...")
#     stock_data = preload_all_stock_data(codes)

#     # 开始逐日回测
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

#     # 统计分析
#     print("\n========== 回测统计 ==========")
#     avg_ret = trades_df["hold_ret"].mean()
#     win_rate = (trades_df["hold_ret"] > 0).mean()
#     avg_excess = trades_df["excess_ret"].mean()
#     total_return = (1 + trades_df["hold_ret"]).prod() - 1 if len(trades_df) > 0 else 0

#     # 按日期分组计算每日平均收益等（可选）
#     print(f"交易总次数: {len(trades_df)}")
#     print(f"平均持有期收益: {avg_ret:.4%}")
#     print(f"胜率: {win_rate:.2%}")
#     print(f"平均超额收益 (相对沪深300): {avg_excess:.4%}")
#     print(f"总收益（按交易序列连乘）: {total_return:.4%}")

#     # 简单最大回撤（按交易顺序）
#     if len(trades_df) > 1:
#         cum_ret = (1 + trades_df["hold_ret"]).cumprod()
#         max_dd = (cum_ret / cum_ret.cummax() - 1).min()
#         print(f"交易序列最大回撤: {max_dd:.4%}")

#     # 按年统计
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
A股趋势策略历史回测脚本（短期数据调试版）

⚠️ 重要警告：
1. 本脚本假设你本地缓存的数据仅有最近约22个交易日（2026-05-19 ~ 2026-06-18）。
2. 回测区间被限制在数据覆盖范围内，且 HOLD_DAYS 被缩短以匹配数据长度。
3. 长周期指标（ma60/ma120等）可能无法计算，筛选标准被大幅放宽。
4. 回测结果无统计学意义，仅用于验证程序流程。

用法：
    python backtest.py
"""

import os
import warnings
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import numpy as np
import pandas as pd
import akshare as ak
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from stock_data import fetch_stock_history, normalize_code

warnings.filterwarnings("ignore")

# ==============================
# 0. 回测参数（已针对短期数据调整）
# ==============================

# 数据覆盖日期：2026-05-19 ~ 2026-06-18（约22个交易日）
# 回测买入日必须 ≥ 数据开始日，且卖出日 ≤ 数据结束日
BACKTEST_START = "2026-05-25"      # 回测开始日期（买入日）
BACKTEST_END   = "2026-06-12"      # 回测结束日期（买入日）
HOLD_DAYS      = 5                 # 持有交易日数（总数据仅22天，不能设太大）

MIN_SCORE           = 75
POOL_STATUSES       = ["强趋势池", "趋势观察池"]
MIN_BARS            = 20           # 最少需要的历史数据天数（设为现有数据量）
MIN_AVG_AMOUNT_20   = 80_000_000   # 流动性门槛

# 基准
BENCHMARK_SYMBOL = "sh000300"
CACHE_DIR = "data"
OUTPUT_DIR = "output"
BENCHMARK_CACHE_FILE = os.path.join(CACHE_DIR, "_benchmark_000300.csv")

MAX_WORKERS = 8
SLEEP_ON_ERROR = 0.5

# ==============================
# 1. 指标计算函数（与原策略一致）
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

    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()

    df["ma20_slope_5"] = df["ma20"] / df["ma20"].shift(5) - 1
    df["ma60_slope_10"] = df["ma60"] / df["ma60"].shift(10) - 1

    df["ret10"] = df["close"] / df["close"].shift(10) - 1
    df["ret20"] = df["close"] / df["close"].shift(20) - 1
    df["ret60"] = df["close"] / df["close"].shift(60) - 1

    df["amount_ma5"] = df["amount"].rolling(5).mean()
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

    if "turnover" in df.columns:
        df["turnover_ma5"] = df["turnover"].rolling(5).mean()
        df["turnover_ma20"] = df["turnover"].rolling(20).mean()
    else:
        df["turnover_ma5"] = np.nan
        df["turnover_ma20"] = np.nan

    df["high20"] = df["high"].rolling(20).max()
    df["high60"] = df["high"].rolling(60).max()
    df["is_20d_high"] = df["close"] >= df["high20"] * 0.999
    df["is_60d_high"] = df["close"] >= df["high60"] * 0.999
    df["dist_to_60d_high"] = df["close"] / df["high60"] - 1

    df["max_dd20"] = np.nan
    if len(df) >= 20:
        for i in range(19, len(df)):
            window = df["close"].iloc[i - 19:i + 1]
            df.loc[df.index[i], "max_dd20"] = max_drawdown(window)

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

    if "pct_chg" in df.columns:
        df["is_limit_up"] = df["pct_chg"] >= 9.5
        df["limit_up_count_5"] = df["is_limit_up"].rolling(5).sum()
    else:
        df["limit_up_count_5"] = np.nan

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
    score = 0
    if row["close"] > row["ma20"]: score += 5
    if row["ma20"] > row["ma60"]: score += 8
    if row["ma60"] > row["ma120"]: score += 7
    if row["ma20_slope_5"] > 0: score += 5
    if row["ma60_slope_10"] > 0: score += 5

    if row["ret20_rank_pct"] >= 0.70: score += 8
    if row["ret60_rank_pct"] >= 0.70: score += 8
    if not pd.isna(bench_ret20):
        if row["ret20"] > bench_ret20 + 0.05: score += 5
        elif row["ret20"] > bench_ret20: score += 3
    if not pd.isna(bench_ret60):
        if row["ret60"] > bench_ret60 + 0.10: score += 4
        elif row["ret60"] > bench_ret60: score += 2

    ratio = row["amount_ratio_5_20"]
    if 1.2 <= ratio <= 4: score += 6
    elif 1.0 <= ratio < 1.2: score += 3
    if row["up_down_amount_ratio20"] > 1.2: score += 4
    elif row["up_down_amount_ratio20"] > 1.0: score += 2
    if (row["is_20d_high"] or row["is_60d_high"]) and ratio >= 1.2: score += 3
    if ratio < 4: score += 2

    if row["is_20d_high"]: score += 4
    if row["is_60d_high"]: score += 6
    if row["dist_to_60d_high"] >= -0.03: score += 3
    elif row["dist_to_60d_high"] >= -0.05: score += 2
    if row["close"] > row["ma20"] and row["dist_to_60d_high"] >= -0.05: score += 2

    max_dd20 = row["max_dd20"]
    if not pd.isna(max_dd20):
        if max_dd20 > -0.10: score += 5
        elif max_dd20 > -0.15: score += 3
        elif max_dd20 > -0.20: score += 1

    overheat = False
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35: overheat = True
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60: overheat = True
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3: overheat = True
    if not overheat: score += 4
    if not row["heavy_bearish_candle"]: score += 3

    turnover_ma5 = row["turnover_ma5"]
    if pd.isna(turnover_ma5): score += 2
    else:
        if turnover_ma5 < 15: score += 3
        elif turnover_ma5 < 25: score += 1

    return int(min(score, 100))


def is_overheat(row: pd.Series) -> bool:
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35: return True
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60: return True
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3: return True
    return False


def classify_status(row: pd.Series) -> str:
    if not row["basic_liquid"]: return "流动性不足"
    if row["close"] < row["ma60"]: return "跌破MA60剔除"
    if row["close"] < row["ma20"]: return "跌破MA20观察"
    if row["heavy_bearish_candle"]: return "放量长阴观察"
    if is_overheat(row): return "短期过热"
    if row["score"] >= 85 and row["candidate"]: return "强趋势池"
    if row["score"] >= MIN_SCORE and row["candidate"]: return "趋势观察池"
    if (row["close"] > row["ma60"] and row["ma20"] > row["ma60"]
            and abs(row["close"] / row["ma20"] - 1) <= 0.05
            and row["ret60_rank_pct"] >= 0.60):
        return "回踩观察"
    return "剔除"


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
            history_days=2000,               # 足够大，实际会使用缓存
            data_dir=CACHE_DIR,
            cache_valid_hours=99999,
            adjust="qfq",
            min_bars=MIN_BARS,               # 只加载数据≥MIN_BARS的股票
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

    # 基准数据截止至当日
    bench_slice = bench_df[bench_df["date"] <= date]
    if len(bench_slice) < 21:   # 至少需要20日前收盘价
        return results

    bench_close_date = bench_slice.iloc[-1]["close"]
    bench_close_20 = bench_slice.iloc[-21]["close"] if len(bench_slice) >= 21 else np.nan
    bench_close_60 = bench_slice.iloc[-61]["close"] if len(bench_slice) >= 61 else np.nan
    bench_ret20 = bench_close_date / bench_close_20 - 1 if not pd.isna(bench_close_20) else np.nan
    bench_ret60 = bench_close_date / bench_close_60 - 1 if not pd.isna(bench_close_60) else np.nan

    # 基准未来持有收益
    future_bench_row = bench_df[bench_df["date"] > date].head(HOLD_DAYS)
    if len(future_bench_row) < HOLD_DAYS:
        return results
    future_bench_close = future_bench_row.iloc[-1]["close"]
    bench_hold_ret = future_bench_close / bench_close_date - 1

    rows = []
    for code, df in stock_data.items():
        # 只使用当日及之前的数据
        df_slice = df[df["date"] <= date].copy()
        if len(df_slice) < MIN_BARS:
            continue

        df_ind = add_indicators(df_slice)
        latest = df_ind.iloc[-1]

        # ⚠️ 放宽核心检查：由于数据短，不强制要求 ma60
        if pd.isna(latest["close"]) or pd.isna(latest["ma20"]) \
                or pd.isna(latest["ret20"]) or pd.isna(latest["amount_ma20"]) \
                or pd.isna(latest["amount_ratio_5_20"]):
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
            "ma20": latest["ma20"],
            "ma60": latest["ma60"] if not pd.isna(latest["ma60"]) else np.nan,
            "ma120": latest["ma120"] if not pd.isna(latest["ma120"]) else np.nan,
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
            "hold_ret": hold_ret,
            "bench_ret20": bench_ret20,
            "bench_ret60": bench_ret60,
            "bench_hold_ret": bench_hold_ret,
        }
        rows.append(row_dict)

    if not rows:
        return results

    df_pool = pd.DataFrame(rows)

    # 全市场排名
    df_pool["ret20_rank_pct"] = df_pool["ret20"].rank(pct=True)
    df_pool["ret60_rank_pct"] = df_pool["ret60"].rank(pct=True)

    df_pool["basic_liquid"] = df_pool["amount_ma20"] >= MIN_AVG_AMOUNT_20

    # 趋势基础条件（ma60/ma120 为 NaN 时自动不满足）
    df_pool["trend_basic"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["ma20"] > df_pool["ma60"])
        & (df_pool["ma60"] > df_pool["ma120"])
        & (df_pool["ma20_slope_5"] > 0)
        & (df_pool["ma60_slope_10"] > 0)
    )

    df_pool["relative_strength"] = (
        (df_pool["ret20_rank_pct"] >= 0.70) & (df_pool["ret60_rank_pct"] >= 0.70)
    )

    df_pool["volume_ok"] = (
        (df_pool["amount_ratio_5_20"] >= 1.2) & (df_pool["amount_ratio_5_20"] <= 4)
    )

    df_pool["near_breakout"] = (
        (df_pool["dist_to_60d_high"] >= -0.05) | df_pool["is_20d_high"] | df_pool["is_60d_high"]
    )

    df_pool["risk_ok"] = (
        (df_pool["close"] > df_pool["ma20"])
        & (df_pool["max_dd20"] > -0.20)
        & (~df_pool["heavy_bearish_candle"])
    )

    df_pool["candidate"] = (
        df_pool["basic_liquid"] & df_pool["trend_basic"] & df_pool["relative_strength"]
        & df_pool["volume_ok"] & df_pool["near_breakout"] & df_pool["risk_ok"]
    )

    df_pool["score"] = df_pool.apply(
        lambda row: calc_score(row, bench_ret20, bench_ret60), axis=1
    )

    df_pool["status"] = df_pool.apply(classify_status, axis=1)

    selected = df_pool[df_pool["status"].isin(POOL_STATUSES)]
    for _, stock in selected.iterrows():
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
    total_return = (1 + trades_df["hold_ret"]).prod() - 1 if len(trades_df) > 0 else 0

    print(f"交易总次数: {len(trades_df)}")
    print(f"平均持有期收益: {avg_ret:.4%}")
    print(f"胜率: {win_rate:.2%}")
    print(f"平均超额收益 (相对沪深300): {avg_excess:.4%}")
    print(f"总收益（按交易序列连乘）: {total_return:.4%}")

    if len(trades_df) > 1:
        cum_ret = (1 + trades_df["hold_ret"]).cumprod()
        max_dd = (cum_ret / cum_ret.cummax() - 1).min()
        print(f"交易序列最大回撤: {max_dd:.4%}")

    trades_df["year"] = pd.to_datetime(trades_df["date"]).dt.year
    yearly = trades_df.groupby("year").agg(
        交易次数=("code", "count"),
        平均收益=("hold_ret", "mean"),
        胜率=("hold_ret", lambda x: (x > 0).mean()),
        平均超额=("excess_ret", "mean"),
    )
    print("\n年度统计:")
    print(yearly.to_string())


if __name__ == "__main__":
    run_backtest()