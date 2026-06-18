# -*- coding: utf-8 -*-

"""
A股趋势型股票池筛选器

功能：
1. 获取A股股票列表
2. 剔除 ST、退市风险、北交所、新股、低流动性股票
3. 拉取日线行情
4. 计算 MA20、MA60、MA120、涨幅排名、成交额放大倍数、距离新高、最大回撤等指标
5. 按趋势型股票池规则打分
6. 输出：
   - 全市场扫描结果 all_scan_result.xlsx
   - 趋势股票池 trend_stock_pool.xlsx

注意：
1. 本代码仅用于研究和辅助筛选，不构成投资建议。
2. AkShare 免费数据接口偶尔会不稳定，失败的股票会自动跳过。
3. 第一次运行可能比较慢，因为需要下载较多股票历史数据。
"""

import os
import time
import warnings
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    # 是否剔除北交所
    exclude_bj: bool = True

    # 下载历史行情线程数
    max_workers: int = 2

    # 请求失败后是否等待
    sleep_on_error: float = 0.2

    # 输出目录
    output_dir: str = "output"

    # 是否使用前复权
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


def safe_float(x):
    try:
        if pd.isna(x):
            return np.nan
        return float(x)
    except Exception:
        return np.nan


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


# ==============================
# 三、获取股票列表
# ==============================

def get_a_stock_universe() -> pd.DataFrame:
    """
    获取A股股票池，并剔除：
    1. ST
    2. 退市风险
    3. 北交所，默认剔除
    4. B股等非主流代码
    """
    print("正在获取A股股票列表...")

    spot = ak.stock_zh_a_spot_em()

    # 东方财富字段一般包括：
    # 代码、名称、最新价、涨跌幅、涨跌额、成交量、成交额、振幅、最高、最低、今开、昨收、量比、换手率、市盈率-动态、市净率、总市值、流通市值、涨速、5分钟涨跌、60日涨跌幅、年初至今涨跌幅
    spot["代码"] = spot["代码"].astype(str).str.zfill(6)
    spot["名称"] = spot["名称"].astype(str)

    df = spot[["代码", "名称"]].copy()
    df.columns = ["code", "name"]

    # 剔除 ST、*ST、退市
    bad_name_keywords = ["ST", "*ST", "退"]
    pattern = "|".join(bad_name_keywords)
    df = df[~df["name"].str.contains(pattern, case=False, regex=True)]

    # 只保留常见沪深A股代码
    # 60：沪市主板
    # 68：科创板
    # 00：深市主板
    # 30：创业板
    # 83/87/88：北交所常见代码，默认剔除
    if CFG.exclude_bj:
        df = df[df["code"].str.startswith(("60", "68", "00", "30"))]

    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)

    print(f"基础股票数量：{len(df)}")
    return df


# ==============================
# 四、获取单只股票历史行情
# ==============================

def fetch_stock_history(code: str) -> pd.DataFrame | None:
    """
    拉取单只股票历史日线数据。
    使用前复权，便于趋势计算。
    """
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date_str(),
            end_date=today_str(),
            adjust=CFG.adjust,
        )

        if df is None or df.empty:
            return None

        # 标准化字段
        rename_map = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "涨跌幅": "pct_chg",
            "涨跌额": "change",
            "换手率": "turnover",
        }

        df = df.rename(columns=rename_map)

        required_cols = ["date", "open", "close", "high", "low", "volume", "amount"]
        for col in required_cols:
            if col not in df.columns:
                return None

        df["date"] = pd.to_datetime(df["date"])

        numeric_cols = [
            "open", "close", "high", "low",
            "volume", "amount", "amplitude",
            "pct_chg", "change", "turnover",
        ]

        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.sort_values("date").reset_index(drop=True)

        if len(df) < CFG.min_bars:
            return None

        return df

    except Exception:
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
        if i < 20:
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
    # 简化规则：涨跌幅 >= 9.5% 视为涨停
    # 创业板/科创板20cm股票这里没有单独区分，主要用于风险提示
    if "pct_chg" in df.columns:
        df["is_limit_up"] = df["pct_chg"] >= 9.5
        df["limit_up_count_5"] = df["is_limit_up"].rolling(5).sum()
    else:
        df["limit_up_count_5"] = np.nan

    # 放量长阴
    # 简化定义：
    # 1. 当日跌幅 <= -5%
    # 2. 当日成交额 > 20日日均成交额 * 1.5
    # 3. 收盘价靠近最低价
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

def process_one_stock(row: pd.Series) -> dict | None:
    code = row["code"]
    name = row["name"]

    hist = fetch_stock_history(code)
    if hist is None or hist.empty:
        return None

    hist = add_indicators(hist)

    latest = hist.iloc[-1].copy()

    # 如果关键指标为空，跳过
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

def get_hs300_return():
    """
    获取沪深300近20日、60日涨幅。
    如果接口失败，返回 NaN。
    """
    try:
        idx = ak.stock_zh_index_daily(symbol="sh000300")
        if idx is None or idx.empty:
            return np.nan, np.nan

        idx["date"] = pd.to_datetime(idx["date"])
        idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
        idx = idx.sort_values("date").reset_index(drop=True)

        if len(idx) < 61:
            return np.nan, np.nan

        ret20 = idx["close"].iloc[-1] / idx["close"].iloc[-21] - 1
        ret60 = idx["close"].iloc[-1] / idx["close"].iloc[-61] - 1

        return ret20, ret60

    except Exception:
        return np.nan, np.nan


# ==============================
# 八、趋势打分
# ==============================

def calc_score(row: pd.Series, bench_ret20: float, bench_ret60: float) -> int:
    """
    趋势型股票池打分，总分100。
    """

    score = 0

    # --------------------------
    # 1. 均线趋势：30分
    # --------------------------
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

    # --------------------------
    # 2. 相对强度：25分
    # --------------------------
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

    # --------------------------
    # 3. 量价配合：15分
    # --------------------------
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

    # --------------------------
    # 4. 突破形态：15分
    # --------------------------
    if row["is_20d_high"]:
        score += 4

    if row["is_60d_high"]:
        score += 6

    if row["dist_to_60d_high"] >= -0.03:
        score += 3
    elif row["dist_to_60d_high"] >= -0.05:
        score += 2

    # 突破后没有明显跌回
    if row["close"] > row["ma20"] and row["dist_to_60d_high"] >= -0.05:
        score += 2

    # --------------------------
    # 5. 风险控制：15分
    # --------------------------
    max_dd20 = row["max_dd20"]

    if not pd.isna(max_dd20):
        if max_dd20 > -0.10:
            score += 5
        elif max_dd20 > -0.15:
            score += 3
        elif max_dd20 > -0.20:
            score += 1

    # 不过热
    is_overheat = False

    if not pd.isna(row["ret10"]) and row["ret10"] > 0.35:
        is_overheat = True

    if not pd.isna(row["ret20"]) and row["ret20"] > 0.60:
        is_overheat = True

    if not pd.isna(row["limit_up_count_5"]) and row["limit_up_count_5"] >= 3:
        is_overheat = True

    if not is_overheat:
        score += 4

    # 没有放量长阴
    if not row["heavy_bearish_candle"]:
        score += 3

    # 换手率没有极端异常
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

    if row["score"] >= 75 and row["candidate"]:
        return "趋势观察池"

    # 回踩观察：趋势还在，回踩20日线附近
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
            except Exception:
                continue

    if len(results) == 0:
        print("没有获取到有效数据，请检查 AkShare 接口或网络。")
        return

    df = pd.DataFrame(results)

    # 剔除停牌太久的数据
    latest_market_date = df["date"].max()
    df = df[(latest_market_date - df["date"]).dt.days <= 7].copy()

    print(f"有效股票数量：{len(df)}")
    print(f"最新交易日期：{latest_market_date.date()}")

    # 全市场涨幅排名
    df["ret20_rank_pct"] = df["ret20"].rank(pct=True)
    df["ret60_rank_pct"] = df["ret60"].rank(pct=True)

    # 获取沪深300作为基准
    hs300_ret20, hs300_ret60 = get_hs300_return()

    # 如果沪深300接口失败，就用全市场中位数作为临时基准
    if pd.isna(hs300_ret20):
        hs300_ret20 = df["ret20"].median()

    if pd.isna(hs300_ret60):
        hs300_ret60 = df["ret60"].median()

    print(f"基准近20日涨幅：{hs300_ret20:.2%}")
    print(f"基准近60日涨幅：{hs300_ret60:.2%}")

    # 基础条件
    df["basic_liquid"] = df["amount_ma20"] >= CFG.min_avg_amount_20

    df["trend_basic"] = (
        (df["close"] > df["ma20"])
        & (df["ma20"] > df["ma60"])
        & (df["ma60"] > df["ma120"])
        & (df["ma20_slope_5"] > 0)
        & (df["ma60_slope_10"] > 0)
    )

    df["relative_strength"] = (
        (df["ret20_rank_pct"] >= 0.70)
        & (df["ret60_rank_pct"] >= 0.70)
    )

    df["volume_ok"] = (
        (df["amount_ratio_5_20"] >= 1.2)
        & (df["amount_ratio_5_20"] <= 4)
    )

    df["near_breakout"] = (
        (df["dist_to_60d_high"] >= -0.05)
        | (df["is_20d_high"])
        | (df["is_60d_high"])
    )

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
        lambda row: calc_score(row, hs300_ret20, hs30060),
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

    # 格式化一些字段，方便看
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
        output_df[col + "_fmt"] = output_df[col].apply(pct_format)

    # 保留主要字段
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

    # 输出全市场扫描结果
    all_path = os.path.join(CFG.output_dir, "all_scan_result.xlsx")
    output_df.to_excel(all_path, index=False)

    # 输出趋势股票池
    pool_statuses = [
        "强趋势池",
        "趋势观察池",
        "回踩观察",
        "短期过热",
    ]

    pool_df = output_df[output_df["status"].isin(pool_statuses)].copy()

    # 如果你只想要严格入池，不想要短期过热，可以打开下面这行：
    # pool_df = pool_df[pool_df["status"].isin(["强趋势池", "趋势观察池", "回踩观察"])]

    pool_path = os.path.join(CFG.output_dir, "trend_stock_pool.xlsx")
    pool_df.to_excel(pool_path, index=False)

    print()
    print("运行完成。")
    print(f"全市场扫描结果：{all_path}")
    print(f"趋势股票池：{pool_path}")
    print()
    print("趋势池数量：")
    print(pool_df["status"].value_counts())


if __name__ == "__main__":
    run()