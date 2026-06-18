

# -*- coding: utf-8 -*-

"""
A股趋势型股票池筛选器（仅主板版）

说明：
1. 本版本仅保留沪深主板股票：
   - 沪市主板：60xxxx
   - 深市主板：00xxxx
2. 自动排除：
   - 科创板：68xxxx
   - 创业板：30xxxx
   - 北交所：83/87/88/43xxxx
   - ST / *ST / 退市风险股
3. 股票列表优先使用 ak.stock_info_a_code_name()
4. 历史行情使用新浪接口 ak.stock_zh_a_daily()
5. 本代码仅用于研究和辅助筛选，不构成投资建议。
"""

import os
import re
import time
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

warnings.filterwarnings("ignore")


# ==============================
# 一、参数配置
# ==============================

@dataclass
class Config:
    # 历史数据起始日期，建议至少 250 个自然日以上
    lookback_days: int = 420

    # 最少需要的交易日数量
    min_bars: int = 130

    # 最近20日日均成交额门槛
    # 8000万 = 80_000_000
    min_avg_amount_20: float = 80_000_000

    # 下载历史行情线程数
    # 免费接口建议不要太高
    max_workers: int = 8

    # 请求失败后的等待时间
    sleep_on_error: float = 0.5

    # 输出目录
    output_dir: str = "output"

    # 是否使用前复权
    # 新浪 stock_zh_a_daily 支持 qfq / hfq / ""
    adjust: str = "qfq"

    # 趋势池最低分
    min_pool_score: int = 75


CFG = Config()


# ==============================
# 二、工具函数
# ==============================

def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def start_date_str() -> str:
    d = datetime.now() - timedelta(days=CFG.lookback_days)
    return d.strftime("%Y%m%d")


def ensure_output_dir():
    Path(CFG.output_dir).mkdir(parents=True, exist_ok=True)


def max_drawdown(series: pd.Series) -> float:
    """
    计算一段价格序列的最大回撤。
    返回值为负数，例如 -0.15 表示最大回撤 15%。
    """
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


def normalize_code(code) -> str:
    """
    统一股票代码为 6 位纯数字。
    兼容：
    600000
    sh600000
    sz000001
    """
    s = str(code).strip()
    m = re.search(r"(\d{6})", s)
    if m:
        return m.group(1)
    return s.zfill(6)


def to_market_symbol(code: str) -> str:
    """
    仅主板：
    600000 -> sh600000
    000001 -> sz000001
    """
    code = normalize_code(code)

    if code.startswith("60"):
        return "sh" + code

    if code.startswith("00"):
        return "sz" + code

    return code


# ==============================
# 三、获取股票列表（仅主板）
# ==============================

def get_a_stock_universe() -> pd.DataFrame:
    """
    获取A股股票池，并仅保留沪深主板：
    1. 剔除 ST / *ST / 退市风险
    2. 剔除科创板、创业板、北交所
    3. 仅保留：
       - 60xxxx 沪市主板
       - 00xxxx 深市主板
    """
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

    # 剔除 ST、*ST、退市
    df = df[~df["name"].str.contains(r"ST|退", case=False, regex=True, na=False)]

    # 仅保留沪深主板
    # 60：沪市主板
    # 00：深市主板
    df = df[df["code"].str.startswith(("60", "00"))]

    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)

    print(f"基础股票数量（仅主板）：{len(df)}")
    return df


# ==============================
# 四、获取单只股票历史行情
# ==============================

def standardize_history_df(raw_df: pd.DataFrame, code: str) -> Optional[pd.DataFrame]:
    """
    标准化行情字段。

    目标字段：
    date, open, close, high, low, volume, amount, pct_chg, change, turnover, amplitude

    新浪 ak.stock_zh_a_daily() 常见字段：
    date, open, high, low, close, volume, amount, outstanding_share, turnover
    """
    if raw_df is None or raw_df.empty:
        return None

    df = raw_df.copy()

    if "date" not in df.columns and "日期" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            first_col = df.columns[0]
            df = df.rename(columns={first_col: "date"})

    rename_map = {
        "日期": "date",
        "时间": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "成交金额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "change",
        "换手率": "turnover",
        "date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume",
        "amount": "amount",
        "turnover": "turnover",
    }

    df = df.rename(columns=rename_map)

    required_cols = ["date", "open", "close", "high", "low", "volume"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        print(f"{code} 缺少必要字段 {missing}，实际字段：{df.columns.tolist()}")
        return None

    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    numeric_cols = [
        "open", "close", "high", "low",
        "volume", "amount", "amplitude",
        "pct_chg", "change", "turnover",
        "outstanding_share",
    ]

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date", "open", "close", "high", "low", "volume"])
    df = df.sort_values("date").reset_index(drop=True)

    if df.empty:
        return None

    # 如果没有 amount，做兜底近似
    if "amount" not in df.columns or df["amount"].isna().all():
        df["amount"] = df["volume"] * df["close"]

    # 如果没有涨跌额，自己计算
    if "change" not in df.columns or df["change"].isna().all():
        df["change"] = df["close"].diff()

    # 如果没有涨跌幅，自己计算，单位为百分比
    if "pct_chg" not in df.columns or df["pct_chg"].isna().all():
        df["pct_chg"] = df["close"].pct_change() * 100

    # 如果没有振幅，自己计算，单位为百分比
    if "amplitude" not in df.columns or df["amplitude"].isna().all():
        pre_close = df["close"].shift(1)
        df["amplitude"] = np.where(
            pre_close > 0,
            (df["high"] - df["low"]) / pre_close * 100,
            np.nan,
        )

    # 如果没有换手率，补 NaN
    if "turnover" not in df.columns:
        df["turnover"] = np.nan

    return df


def fetch_stock_history(code: str) -> Optional[pd.DataFrame]:
    """
    拉取单只股票历史日线数据。
    使用新浪 stock_zh_a_daily()。
    """
    try:
        symbol = to_market_symbol(code)

        raw_df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_date_str(),
            end_date=today_str(),
            adjust=CFG.adjust,
        )

        df = standardize_history_df(raw_df, code)

        if df is None or df.empty:
            return None

        if len(df) < CFG.min_bars:
            return None

        return df

    except Exception as e:
        print(f"{code} 下载失败：{e}")
        time.sleep(CFG.sleep_on_error)
        return None


# ==============================
# 五、计算技术指标
# ==============================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    给日线数据添加趋势指标。
    """
    df = df.copy()

    df["pre_close"] = df["close"].shift(1)

    # 均线
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()

    # 均线斜率
    df["ma20_slope_5"] = df["ma20"] / df["ma20"].shift(5) - 1
    df["ma60_slope_10"] = df["ma60"] / df["ma60"].shift(10) - 1

    # 涨幅
    df["ret10"] = df["close"] / df["close"].shift(10) - 1
    df["ret20"] = df["close"] / df["close"].shift(20) - 1
    df["ret60"] = df["close"] / df["close"].shift(60) - 1

    # 成交额均值
    df["amount_ma5"] = df["amount"].rolling(5).mean()
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20"]

    # 换手率均值
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

    # 最近20日最大回撤
    df["max_dd20"] = np.nan
    if len(df) >= 20:
        for i in range(19, len(df)):
            window = df["close"].iloc[i - 19:i + 1]
            df.loc[df.index[i], "max_dd20"] = max_drawdown(window)

    # 上涨日成交额 / 下跌日成交额
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

    # 最近5日涨停数量
    if "pct_chg" in df.columns:
        df["is_limit_up"] = df["pct_chg"] >= 9.5
        df["limit_up_count_5"] = df["is_limit_up"].rolling(5).sum()
    else:
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


# ==============================
# 六、处理单只股票
# ==============================

def process_one_stock(row: pd.Series) -> Optional[dict]:
    code = row["code"]
    name = row["name"]

    hist = fetch_stock_history(code)
    if hist is None or hist.empty:
        return None

    hist = add_indicators(hist)
    latest = hist.iloc[-1].copy()

    key_cols = [
        "close", "ma20", "ma60", "ma120",
        "ret20", "ret60",
        "amount_ma20", "amount_ratio_5_20",
        "high60", "dist_to_60d_high",
    ]

    for col in key_cols:
        if col not in latest or pd.isna(latest[col]):
            return None

    result = {
        "code": code,
        "name": name,
        "date": latest["date"],

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
    }

    return result


# ==============================
# 七、获取沪深300作为基准
# ==============================

def get_hs300_return() -> Tuple[float, float]:
    """
    获取沪深300近20日、60日涨幅。
    如果接口失败，返回 NaN。
    """
    try:
        idx = ak.stock_zh_index_daily(symbol="sh000300")

        if idx is None or idx.empty:
            return np.nan, np.nan

        if "date" not in idx.columns and "日期" in idx.columns:
            idx = idx.rename(columns={"日期": "date"})

        if "close" not in idx.columns and "收盘" in idx.columns:
            idx = idx.rename(columns={"收盘": "close"})

        if "date" not in idx.columns or "close" not in idx.columns:
            print(f"沪深300字段异常：{idx.columns.tolist()}")
            return np.nan, np.nan

        idx["date"] = pd.to_datetime(idx["date"], errors="coerce")
        idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
        idx = idx.dropna(subset=["date", "close"])
        idx = idx.sort_values("date").reset_index(drop=True)

        if len(idx) < 61:
            return np.nan, np.nan

        ret20 = idx["close"].iloc[-1] / idx["close"].iloc[-21] - 1
        ret60 = idx["close"].iloc[-1] / idx["close"].iloc[-61] - 1

        return ret20, ret60

    except Exception as e:
        print(f"沪深300获取失败：{e}")
        return np.nan, np.nan


# ==============================
# 八、趋势打分
# ==============================

def calc_score(row: pd.Series, bench_ret20: float, bench_ret60: float) -> int:
    """
    趋势型股票池打分，总分100。
    """
    score = 0

    # 1. 均线趋势：30分
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

    # 2. 相对强度：25分
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

    # 3. 量价配合：15分
    ratio = row["amount_ratio_5_20"]

    if 1.2 <= ratio <= 4:
        score += 6
    elif 1.0 <= ratio < 1.2:
        score += 3

    if row["up_down_amount_ratio20"] > 1.2:
        score += 4
    elif row["up_down_amount_ratio20"] > 1.0:
        score += 2

    if row["is_20d_high"] or row["is_60d_high"]:
        if ratio >= 1.2:
            score += 3

    if ratio < 4:
        score += 2

    # 4. 突破形态：15分
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

    # 5. 风险控制：15分
    max_dd20 = row["max_dd20"]
    if not pd.isna(max_dd20):
        if max_dd20 > -0.10:
            score += 5
        elif max_dd20 > -0.15:
            score += 3
        elif max_dd20 > -0.20:
            score += 1

    overheat = False
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        overheat = True
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        overheat = True
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        overheat = True

    if not overheat:
        score += 4

    if not row["heavy_bearish_candle"]:
        score += 3

    turnover_ma5 = row["turnover_ma5"]
    if pd.isna(turnover_ma5):
        score += 2
    else:
        if turnover_ma5 < 15:
            score += 3
        elif turnover_ma5 < 25:
            score += 1

    return int(min(score, 100))


# ==============================
# 九、生成标签和入池状态
# ==============================

def build_risk_tags(row: pd.Series) -> str:
    tags = []

    if row["heavy_bearish_candle"]:
        tags.append("放量长阴")
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        tags.append("10日涨幅过热")
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        tags.append("20日涨幅过热")
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        tags.append("连续涨停风险")
    if not pd.isna(row["max_dd20"]) and row["max_dd20"] < -0.20:
        tags.append("20日回撤过大")
    if not pd.isna(row["turnover_ma5"]) and row["turnover_ma5"] > 25:
        tags.append("高换手")
    if row["close"] < row["ma20"]:
        tags.append("跌破MA20")
    if row["close"] < row["ma60"]:
        tags.append("跌破MA60")

    if len(tags) == 0:
        return "无明显技术风险"

    return "、".join(tags)


def is_overheat(row: pd.Series) -> bool:
    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        return True
    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        return True
    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        return True
    return False


def classify_status(row: pd.Series) -> str:
    """
    给股票分类。
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

    if row["score"] >= CFG.min_pool_score and row["candidate"]:
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
# 十、主流程
# ==============================

def run():
    ensure_output_dir()

    universe = get_a_stock_universe()

    # universe = universe.head(500)

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

    # 剔除停牌太久的数据
    latest_market_date = df["date"].max()
    df = df[(latest_market_date - df["date"]).dt.days <= 7].copy()

    if df.empty:
        print("有效数据为空，可能是行情日期异常或接口数据异常。")
        return

    print(f"有效股票数量：{len(df)}")
    print(f"最新交易日期：{latest_market_date.date()}")

    # 全市场涨幅排名
    df["ret20_rank_pct"] = df["ret20"].rank(pct=True)
    df["ret60_rank_pct"] = df["ret60"].rank(pct=True)

    # 获取沪深300作为基准
    hs300_ret20, hs300_ret60 = get_hs300_return()

    if pd.isna(hs300_ret20):
        hs300_ret20 = df["ret20"].median()
    if pd.isna(hs300_ret60):
        hs300_ret60 = df["ret60"].median()

    print(f"基准近20日涨幅：{hs300_ret20:.2%}")
    print(f"基准近60日涨幅：{hs300_ret60:.2%}")

    # 基础流动性
    df["basic_liquid"] = df["amount_ma20"] >= CFG.min_avg_amount_20

    # 趋势基础条件
    df["trend_basic"] = (
        (df["close"] > df["ma20"])
        & (df["ma20"] > df["ma60"])
        & (df["ma60"] > df["ma120"])
        & (df["ma20_slope_5"] > 0)
        & (df["ma60_slope_10"] > 0)
    )

    # 相对强度
    df["relative_strength"] = (
        (df["ret20_rank_pct"] >= 0.70)
        & (df["ret60_rank_pct"] >= 0.70)
    )

    # 成交额配合
    df["volume_ok"] = (
        (df["amount_ratio_5_20"] >= 1.2)
        & (df["amount_ratio_5_20"] <= 4)
    )

    # 接近突破或已突破
    df["near_breakout"] = (
        (df["dist_to_60d_high"] >= -0.05)
        | (df["is_20d_high"])
        | (df["is_60d_high"])
    )

    # 风险过滤
    df["risk_ok"] = (
        (df["close"] > df["ma20"])
        & (df["max_dd20"] > -0.20)
        & (~df["heavy_bearish_candle"])
    )

    # 候选条件
    df["candidate"] = (
        df["basic_liquid"]
        & df["trend_basic"]
        & df["relative_strength"]
        & df["volume_ok"]
        & df["near_breakout"]
        & df["risk_ok"]
    )

    # 评分
    df["score"] = df.apply(
        lambda row: calc_score(row, hs300_ret20, hs300_ret60),
        axis=1,
    )

    # 风险标签
    df["risk_tags"] = df.apply(build_risk_tags, axis=1)

    # 分类
    df["status"] = df.apply(classify_status, axis=1)

    # 排序
    df = df.sort_values(
        by=["score", "ret20_rank_pct", "ret60_rank_pct"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    output_df = df.copy()

    percent_cols = [
        "ma20_slope_5",
        "ma60_slope_10",
        "ret10",
        "ret20",
        "ret60",
        "dist_to_60d_high",
        "max_dd20",
        "ret20_rank_pct",
        "ret60_rank_pct",
    ]

    for col in percent_cols:
        if col in output_df.columns:
            output_df[col + "_fmt"] = output_df[col].apply(pct_format)

    final_cols = [
        "date",
        "code",
        "name",
        "close",

        "score",
        "status",
        "risk_tags",

        "ret10_fmt",
        "ret20_fmt",
        "ret60_fmt",
        "ret20_rank_pct_fmt",
        "ret60_rank_pct_fmt",

        "ma20",
        "ma60",
        "ma120",
        "ma20_slope_5_fmt",
        "ma60_slope_10_fmt",

        "amount_ma20",
        "amount_ratio_5_20",
        "up_down_amount_ratio20",

        "dist_to_60d_high_fmt",
        "is_20d_high",
        "is_60d_high",
        "max_dd20_fmt",

        "turnover_ma5",
        "limit_up_count_5",

        "basic_liquid",
        "trend_basic",
        "relative_strength",
        "volume_ok",
        "near_breakout",
        "risk_ok",
        "candidate",
    ]

    final_cols = [col for col in final_cols if col in output_df.columns]
    output_df = output_df[final_cols]

    all_path = os.path.join(CFG.output_dir, "all_scan_result.xlsx")
    output_df.to_excel(all_path, index=False)

    pool_statuses = [
        "强趋势池",
        "趋势观察池",
        "回踩观察",
        "短期过热",
    ]

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
