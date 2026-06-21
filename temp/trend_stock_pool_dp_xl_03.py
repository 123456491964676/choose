# -*- coding: utf-8 -*-

"""
A股趋势型股票池筛选器（仅主板版）—— 短期数据适配版

说明：
- 为适配 ~22 个交易日的历史数据，将技术指标周期缩短至 ma5、ma10 等。
- 结果仅供参考，不具备实际投资指导意义。
- 如需完整功能，请用 stock_data.py 下载 1825 天数据并恢复原始参数。
"""

import os
import warnings
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import akshare as ak
from tqdm import tqdm

from stock_data import fetch_stock_history, normalize_code

warnings.filterwarnings("ignore")


@dataclass
class Config:
    history_days: int = 30            # 历史天数（缓存足够大即可）
    min_bars: int = 10                # 至少20天，确保能算 ma20
    min_avg_amount_20: float = 80_000_000
    max_workers: int = 8
    sleep_on_error: float = 0.5
    output_dir: str = "output"
    data_dir: str = "data"
    cache_valid_hours: int = 30000    # 始终用缓存
    adjust: str = "qfq"
    min_pool_score: int = 75


CFG = Config()


def ensure_output_dir():
    Path(CFG.output_dir).mkdir(parents=True, exist_ok=True)


def max_drawdown(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) == 0:
        return np.nan
    cum_max = s.cummax()
    dd = s / cum_max - 1
    return dd.min()


def pct_format(x):
    if pd.isna(x):
        return ""
    return f"{x * 100:.2f}%"


def get_a_stock_universe() -> pd.DataFrame:
    print("正在获取A股股票列表...")
    stock_info = None
    last_err = None
    try:
        stock_info = ak.stock_info_a_code_name()
    except Exception as e:
        last_err = e
        stock_info = None
    if stock_info is None or stock_info.empty:
        try:
            stock_info = ak.stock_zh_a_spot()
        except Exception as e:
            last_err = e
            stock_info = None
    if stock_info is None or stock_info.empty:
        raise RuntimeError(f"无法获取股票列表，请检查 AkShare 或网络。最后错误：{last_err}")

    if "code" in stock_info.columns and "name" in stock_info.columns:
        df = stock_info[["code", "name"]].copy()
    elif "代码" in stock_info.columns and "名称" in stock_info.columns:
        df = stock_info[["代码", "名称"]].copy()
        df.columns = ["code", "name"]
    elif "symbol" in stock_info.columns and "name" in stock_info.columns:
        df = stock_info[["symbol", "name"]].copy()
        df.columns = ["code", "name"]
    else:
        raise ValueError(f"股票列表字段异常，实际字段为：{stock_info.columns.tolist()}")

    df["code"] = df["code"].apply(normalize_code)
    df["name"] = df["name"].astype(str)
    df = df[~df["name"].str.contains(r"ST|退", case=False, regex=True, na=False)]
    df = df[df["code"].str.startswith(("60", "00"))]
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    print(f"基础股票数量（仅主板）：{len(df)}")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pre_close"] = df["close"].shift(1)

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    df["ma5_slope_2"] = df["ma5"] / df["ma5"].shift(2) - 1
    df["ma10_slope_3"] = df["ma10"] / df["ma10"].shift(3) - 1

    df["ret3"] = df["close"] / df["close"].shift(3) - 1
    df["ret5"] = df["close"] / df["close"].shift(5) - 1
    df["ret10"] = df["close"] / df["close"].shift(10) - 1
    df["ret20"] = df["close"] / df["close"].shift(20) - 1

    df["amount_ma5"] = df["amount"].rolling(5).mean()
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

    if "turnover" in df.columns:
        df["turnover_ma5"] = df["turnover"].rolling(5).mean()
        df["turnover_ma10"] = df["turnover"].rolling(10).mean()
    else:
        df["turnover_ma5"] = np.nan
        df["turnover_ma10"] = np.nan

    df["high5"] = df["high"].rolling(5).max()
    df["high10"] = df["high"].rolling(10).max()
    df["high20"] = df["high"].rolling(20).max()
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


def process_one_stock(row: pd.Series) -> Optional[dict]:
    code = row["code"]
    name = row["name"]

    hist = fetch_stock_history(
        code,
        history_days=CFG.history_days,
        data_dir=CFG.data_dir,
        cache_valid_hours=CFG.cache_valid_hours,
        adjust=CFG.adjust,
        min_bars=CFG.min_bars,
        sleep_on_error=CFG.sleep_on_error,
    )
    if hist is None or hist.empty:
        return None

    hist = add_indicators(hist)
    latest = hist.iloc[-1].copy()

    core_cols = [
        "close", "ma5", "ma10",
        "ret5", "ret10",
        "amount_ma20", "amount_ratio_5_20",
    ]
    for col in core_cols:
        if col not in latest or pd.isna(latest[col]):
            return None

    def safe_get(col, default=np.nan):
        return latest[col] if col in latest and not pd.isna(latest[col]) else default

    result = {
        "code": code,
        "name": name,
        "date": latest["date"],

        "close": latest["close"],
        "ma5": latest["ma5"],
        "ma10": latest["ma10"],
        "ma20": safe_get("ma20"),

        "ma5_slope_2": safe_get("ma5_slope_2"),
        "ma10_slope_3": safe_get("ma10_slope_3"),

        "ret3": safe_get("ret3"),
        "ret5": latest["ret5"],
        "ret10": latest["ret10"],
        "ret20": safe_get("ret20"),

        "amount_ma5": safe_get("amount_ma5"),
        "amount_ma20": latest["amount_ma20"],
        "amount_ratio_5_20": latest["amount_ratio_5_20"],

        "turnover_ma5": latest.get("turnover_ma5", np.nan),
        "turnover_ma10": latest.get("turnover_ma10", np.nan),

        "high5": safe_get("high5"),
        "high10": safe_get("high10"),
        "high20": safe_get("high20"),
        "is_5d_high": bool(latest["is_5d_high"]) if "is_5d_high" in latest and not pd.isna(latest["is_5d_high"]) else False,
        "is_10d_high": bool(latest["is_10d_high"]) if "is_10d_high" in latest and not pd.isna(latest["is_10d_high"]) else False,
        "dist_to_10d_high": safe_get("dist_to_10d_high"),

        "max_dd10": safe_get("max_dd10"),
        "up_down_amount_ratio10": safe_get("up_down_amount_ratio10"),
        "limit_up_count_3": latest.get("limit_up_count_3", np.nan),
        "heavy_bearish_candle": bool(latest.get("heavy_bearish_candle", False)),
    }

    return result


def get_hs300_returns() -> Tuple[float, float]:
    try:
        idx = ak.stock_zh_index_daily(symbol="sh000300")
        if idx is None or idx.empty:
            return np.nan, np.nan
        if "date" not in idx.columns and "日期" in idx.columns:
            idx = idx.rename(columns={"日期": "date"})
        if "close" not in idx.columns and "收盘" in idx.columns:
            idx = idx.rename(columns={"收盘": "close"})
        idx["date"] = pd.to_datetime(idx["date"], errors="coerce")
        idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
        idx = idx.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
        if len(idx) < 11:
            return np.nan, np.nan
        ret5 = idx["close"].iloc[-1] / idx["close"].iloc[-6] - 1
        ret10 = idx["close"].iloc[-1] / idx["close"].iloc[-11] - 1
        return ret5, ret10
    except Exception as e:
        print(f"沪深300获取失败：{e}")
        return np.nan, np.nan


def calc_score(row: pd.Series, bench_ret5: float, bench_ret10: float) -> int:
    score = 0
    if row["close"] > row["ma5"]: score += 5
    if row["ma5"] > row["ma10"]: score += 8
    if row["ma10"] > row["ma20"]: score += 7
    if row["ma5_slope_2"] > 0: score += 5
    if row["ma10_slope_3"] > 0: score += 5

    if row["ret5_rank_pct"] >= 0.70: score += 8
    if row["ret10_rank_pct"] >= 0.70: score += 8

    if not pd.isna(bench_ret5):
        if row["ret5"] > bench_ret5 + 0.02: score += 5
        elif row["ret5"] > bench_ret5: score += 3
    if not pd.isna(bench_ret10):
        if row["ret10"] > bench_ret10 + 0.05: score += 4
        elif row["ret10"] > bench_ret10: score += 2

    ratio = row["amount_ratio_5_20"]
    if 1.2 <= ratio <= 4: score += 6
    elif 1.0 <= ratio < 1.2: score += 3
    if row["up_down_amount_ratio10"] > 1.2: score += 4
    elif row["up_down_amount_ratio10"] > 1.0: score += 2
    if (row["is_5d_high"] or row["is_10d_high"]) and ratio >= 1.2: score += 3
    if ratio < 4: score += 2

    if row["is_5d_high"]: score += 4
    if row["is_10d_high"]: score += 6
    if row["dist_to_10d_high"] >= -0.03: score += 3
    elif row["dist_to_10d_high"] >= -0.05: score += 2
    if row["close"] > row["ma5"] and row["dist_to_10d_high"] >= -0.05: score += 2

    max_dd10 = row["max_dd10"]
    if not pd.isna(max_dd10):
        if max_dd10 > -0.05: score += 5
        elif max_dd10 > -0.10: score += 3
        elif max_dd10 > -0.15: score += 1

    overheat = False
    if not pd.isna(row["ret3"]) and row["ret3"] > 0.15: overheat = True
    if not pd.isna(row["ret5"]) and row["ret5"] > 0.25: overheat = True
    if not pd.isna(row["limit_up_count_3"]) and row["limit_up_count_3"] >= 2: overheat = True
    if not overheat: score += 4
    if not row["heavy_bearish_candle"]: score += 3

    turnover_ma5 = row["turnover_ma5"]
    if pd.isna(turnover_ma5): score += 2
    else:
        if turnover_ma5 < 15: score += 3
        elif turnover_ma5 < 25: score += 1

    return int(min(score, 100))


def build_risk_tags(row: pd.Series) -> str:
    tags = []
    if row["heavy_bearish_candle"]: tags.append("放量长阴")
    if not pd.isna(row["ret3"]) and row["ret3"] > 0.15: tags.append("3日涨幅过热")
    if not pd.isna(row["ret5"]) and row["ret5"] > 0.25: tags.append("5日涨幅过热")
    if not pd.isna(row["limit_up_count_3"]) and row["limit_up_count_3"] >= 2: tags.append("连续涨停风险")
    if not pd.isna(row["max_dd10"]) and row["max_dd10"] < -0.15: tags.append("10日回撤过大")
    if not pd.isna(row["turnover_ma5"]) and row["turnover_ma5"] > 25: tags.append("高换手")
    if row["close"] < row["ma5"]: tags.append("跌破MA5")
    if row["close"] < row["ma10"]: tags.append("跌破MA10")
    return "、".join(tags) if tags else "无明显技术风险"


def is_overheat(row: pd.Series) -> bool:
    if not pd.isna(row["ret3"]) and row["ret3"] > 0.15: return True
    if not pd.isna(row["ret5"]) and row["ret5"] > 0.25: return True
    if not pd.isna(row["limit_up_count_3"]) and row["limit_up_count_3"] >= 2: return True
    return False


def classify_status(row: pd.Series) -> str:
    if not row["basic_liquid"]: return "流动性不足"
    if row["close"] < row["ma10"]: return "跌破MA10剔除"
    if row["close"] < row["ma5"]: return "跌破MA5观察"
    if row["heavy_bearish_candle"]: return "放量长阴观察"
    if is_overheat(row): return "短期过热"
    if row["score"] >= 85 and row["candidate"]: return "强趋势池"
    if row["score"] >= CFG.min_pool_score and row["candidate"]: return "趋势观察池"
    if (row["close"] > row["ma10"] and row["ma5"] > row["ma10"]
            and abs(row["close"] / row["ma5"] - 1) <= 0.05
            and row["ret10_rank_pct"] >= 0.60):
        return "回踩观察"
    return "剔除"


def run():
    ensure_output_dir()
    universe = get_a_stock_universe()
    print("开始下载并计算个股趋势指标...")
    results = []

    with ThreadPoolExecutor(max_workers=CFG.max_workers) as executor:
        futures = {
            executor.submit(process_one_stock, row): row["code"]
            for _, row in universe.iterrows()
        }
        for future in tqdm(as_completed(futures), total=len(futures)):
            code = futures[future]
            try:
                res = future.result()
                if res is not None:
                    results.append(res)
            except Exception as e:
                print(f"{code} 处理失败：{e}")
                continue

    if len(results) == 0:
        print("没有获取到有效数据，请检查 AkShare 接口或网络。")
        return

    df = pd.DataFrame(results)
    latest_market_date = df["date"].max()
    df = df[(latest_market_date - df["date"]).dt.days <= 7].copy()
    if df.empty:
        print("有效数据为空，可能是行情日期异常或接口数据异常。")
        return

    print(f"有效股票数量：{len(df)}")
    print(f"最新交易日期：{latest_market_date.date()}")

    df["ret5_rank_pct"] = df["ret5"].rank(pct=True)
    df["ret10_rank_pct"] = df["ret10"].rank(pct=True)

    hs300_ret5, hs300_ret10 = get_hs300_returns()
    if pd.isna(hs300_ret5):
        hs300_ret5 = df["ret5"].median()
    if pd.isna(hs300_ret10):
        hs300_ret10 = df["ret10"].median()
    print(f"基准近5日涨幅：{hs300_ret5:.2%}")
    print(f"基准近10日涨幅：{hs300_ret10:.2%}")

    df["basic_liquid"] = df["amount_ma20"] >= CFG.min_avg_amount_20

    df["trend_basic"] = (
        (df["close"] > df["ma5"])
        & (df["ma5"] > df["ma10"])
        & (df["ma10"] > df["ma20"])
        & (df["ma5_slope_2"] > 0)
        & (df["ma10_slope_3"] > 0)
    )

    df["relative_strength"] = (
        (df["ret5_rank_pct"] >= 0.70) & (df["ret10_rank_pct"] >= 0.70)
    )

    df["volume_ok"] = (
        (df["amount_ratio_5_20"] >= 1.2) & (df["amount_ratio_5_20"] <= 4)
    )

    df["near_breakout"] = (
        (df["dist_to_10d_high"] >= -0.05) | df["is_5d_high"] | df["is_10d_high"]
    )

    df["risk_ok"] = (
        (df["close"] > df["ma5"])
        & (df["max_dd10"] > -0.20)
        & (~df["heavy_bearish_candle"])
    )

    df["candidate"] = (
        df["basic_liquid"] & df["trend_basic"] & df["relative_strength"]
        & df["volume_ok"] & df["near_breakout"] & df["risk_ok"]
    )

    df["score"] = df.apply(lambda row: calc_score(row, hs300_ret5, hs300_ret10), axis=1)
    df["risk_tags"] = df.apply(build_risk_tags, axis=1)
    df["status"] = df.apply(classify_status, axis=1)

    df = df.sort_values(
        by=["score", "ret5_rank_pct", "ret10_rank_pct"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    output_df = df.copy()
    percent_cols = [
        "ma5_slope_2", "ma10_slope_3", "ret3", "ret5", "ret10", "ret20",
        "dist_to_10d_high", "max_dd10", "ret5_rank_pct", "ret10_rank_pct",
    ]
    for col in percent_cols:
        if col in output_df.columns:
            output_df[col + "_fmt"] = output_df[col].apply(pct_format)

    final_cols = [
        "date", "code", "name", "close",
        "score", "status", "risk_tags",
        "ret3_fmt", "ret5_fmt", "ret10_fmt", "ret5_rank_pct_fmt", "ret10_rank_pct_fmt",
        "ma5", "ma10", "ma20", "ma5_slope_2_fmt", "ma10_slope_3_fmt",
        "amount_ma20", "amount_ratio_5_20", "up_down_amount_ratio10",
        "dist_to_10d_high_fmt", "is_5d_high", "is_10d_high", "max_dd10_fmt",
        "turnover_ma5", "limit_up_count_3",
        "basic_liquid", "trend_basic", "relative_strength",
        "volume_ok", "near_breakout", "risk_ok", "candidate",
    ]
    final_cols = [col for col in final_cols if col in output_df.columns]
    output_df = output_df[final_cols]

    all_path = os.path.join(CFG.output_dir, "all_scan_result.xlsx")
    output_df.to_excel(all_path, index=False)

    pool_statuses = ["强趋势池", "趋势观察池", "回踩观察", "短期过热"]
    pool_df = output_df[output_df["status"].isin(pool_statuses)].copy()
    pool_path = os.path.join(CFG.output_dir, "trend_stock_pool.xlsx")
    pool_df.to_excel(pool_path, index=False)

    output_df.to_csv(os.path.join(CFG.output_dir, "all_scan_result.csv"), index=False, encoding="utf-8-sig")
    pool_df.to_csv(os.path.join(CFG.output_dir, "trend_stock_pool.csv"), index=False, encoding="utf-8-sig")

    print()
    print("运行完成。")
    print(f"全市场扫描结果：{all_path}")
    print(f"趋势股票池：{pool_path}")
    print()
    if not pool_df.empty:
        print("趋势池数量：")
        print(pool_df["status"].value_counts())
    else:
        print("趋势池为空。")


if __name__ == "__main__":
    run()