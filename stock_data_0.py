#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A股历史日线数据下载与缓存模块

功能：
- 单独运行：下载指定股票或全部主板股票的历史行情，并缓存到本地。
- 作为模块：调用 fetch_stock_history() 获取单只股票数据。

用法示例：
    python stock_data.py --code 600000,000001
    python stock_data.py --all --days 1095 --workers 8
"""

import os
import re
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import akshare as ak
from tqdm import tqdm


# ==============================
# 工具函数
# ==============================

def normalize_code(code) -> str:
    """统一股票代码为 6 位纯数字"""
    s = str(code).strip()
    m = re.search(r"(\d{6})", s)
    if m:
        return m.group(1)
    return s.zfill(6)


def to_market_symbol(code: str) -> str:
    """仅主板：600000 -> sh600000, 000001 -> sz000001"""
    code = normalize_code(code)
    if code.startswith("60"):
        return "sh" + code
    if code.startswith("00"):
        return "sz" + code
    return code


def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def start_date_str(history_days: int = 30) -> str:
    """根据回溯天数计算起始日期，默认30天"""
    d = datetime.now() - timedelta(days=history_days)
    return d.strftime("%Y%m%d")


def ensure_data_dir(data_dir: str = "data"):
    Path(data_dir).mkdir(parents=True, exist_ok=True)


# def is_cache_valid(file_path: str, cache_valid_hours: int = 12) -> bool:
#     """判断缓存文件是否在有效期内"""
#     if not os.path.exists(file_path):
#         return False
#     mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
#     now = datetime.now()
#     return (now - mtime) < timedelta(hours=cache_valid_hours)
def is_cache_valid(file_path: str, cache_valid_hours: int = 12) -> bool:
    """判断缓存文件是否在有效期内，cache_valid_hours<=0 表示永久有效"""
    if not os.path.exists(file_path):
        return False
    # if cache_valid_hours <= 0:
    #     return True
    mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
    now = datetime.now()
    return (now - mtime) < timedelta(hours=cache_valid_hours)

# ==============================
# 数据标准化
# ==============================

def standardize_history_df(raw_df: pd.DataFrame, code: str) -> Optional[pd.DataFrame]:
    """
    将 ak.stock_zh_a_daily 返回的数据标准化为统一格式。
    目标字段：date, open, close, high, low, volume, amount,
             pct_chg, change, turnover, amplitude
    """
    if raw_df is None or raw_df.empty:
        return None

    df = raw_df.copy()

    # 如果索引为日期，重置并重命名
    if "date" not in df.columns and "日期" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            first_col = df.columns[0]
            df = df.rename(columns={first_col: "date"})

    rename_map = {
        "日期": "date", "时间": "date",
        "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount", "成交金额": "amount",
        "振幅": "amplitude", "涨跌幅": "pct_chg", "涨跌额": "change",
        "换手率": "turnover",
        "date": "date", "open": "open", "close": "close",
        "high": "high", "low": "low", "volume": "volume",
        "amount": "amount", "turnover": "turnover",
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

    # 补充缺失的衍生字段
    if "amount" not in df.columns or df["amount"].isna().all():
        df["amount"] = df["volume"] * df["close"]

    if "change" not in df.columns or df["change"].isna().all():
        df["change"] = df["close"].diff()

    if "pct_chg" not in df.columns or df["pct_chg"].isna().all():
        df["pct_chg"] = df["close"].pct_change() * 100

    if "amplitude" not in df.columns or df["amplitude"].isna().all():
        pre_close = df["close"].shift(1)
        df["amplitude"] = np.where(
            pre_close > 0,
            (df["high"] - df["low"]) / pre_close * 100,
            np.nan,
        )

    if "turnover" not in df.columns:
        df["turnover"] = np.nan

    return df


# ==============================
# 带缓存的历史行情获取
# ==============================

def fetch_stock_history(
    code: str,
    history_days: int = 30,
    data_dir: str = "data",
    cache_valid_hours: int = 12,
    adjust: str = "qfq",
    min_bars: int = 130,
    sleep_on_error: float = 0.5,
) -> Optional[pd.DataFrame]:
    """
    拉取单只股票历史日线数据，优先使用本地缓存。

    参数：
        code:               6位股票代码，如 '600000'
        history_days:       回溯天数，默认30天
        data_dir:           缓存目录
        cache_valid_hours:  缓存有效时间（小时），超过后重新下载
        adjust:             复权方式（'qfq'前复权，''不复权）
        min_bars:           最少需要的交易日数量，不足返回None
        sleep_on_error:     网络出错后等待秒数

    返回：
        标准化的 DataFrame（包含 date, open, close, high, low 等），或 None
    """
    ensure_data_dir(data_dir)
    cache_path = os.path.join(data_dir, f"{code}.csv")

    # 如果缓存有效，直接读取
    if is_cache_valid(cache_path, cache_valid_hours):
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"])
            # if df is not None and not df.empty and len(df) >= min_bars:
            if df is not None and not df.empty:
                print("ok")
                return df
        except Exception:
            pass  # 缓存损坏，继续下载

    # 需要下载
    try:
        print("NO")
        symbol = to_market_symbol(code)
        raw_df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_date_str(history_days),
            end_date=today_str(),
            adjust=adjust,
        )

        df = standardize_history_df(raw_df, code)

        if df is not None and not df.empty:
            df.to_csv(cache_path, index=False)
            if len(df) < min_bars:
                return None
            return df

        # 下载成功但数据为空，尝试用旧缓存
        if os.path.exists(cache_path):
            print(f"{code} 下载为空，使用旧缓存")
            return pd.read_csv(cache_path, parse_dates=["date"])
        return None

    except Exception as e:
        print(f"{code} 下载失败：{e}")
        # 失败时如果有旧缓存，则使用
        if os.path.exists(cache_path):
            print(f"{code} 使用本地缓存数据")
            try:
                return pd.read_csv(cache_path, parse_dates=["date"])
            except Exception:
                return None
        time.sleep(sleep_on_error)
        return None


# ==============================
# 获取主板股票列表（用于 --all 模式）
# ==============================

def get_mainboard_stocks() -> pd.DataFrame:
    """获取沪深主板股票列表，剔除ST，返回 DataFrame(code, name)"""
    print("正在获取A股股票列表...")
    stock_info = None
    try:
        stock_info = ak.stock_info_a_code_name()
    except Exception:
        try:
            stock_info = ak.stock_zh_a_spot()
        except Exception as e:
            raise RuntimeError(f"无法获取股票列表：{e}")

    if stock_info is None or stock_info.empty:
        raise RuntimeError("股票列表为空")

    # 统一列名
    if "code" in stock_info.columns and "name" in stock_info.columns:
        df = stock_info[["code", "name"]].copy()
    elif "代码" in stock_info.columns and "名称" in stock_info.columns:
        df = stock_info[["代码", "名称"]].copy()
        df.columns = ["code", "name"]
    elif "symbol" in stock_info.columns and "name" in stock_info.columns:
        df = stock_info[["symbol", "name"]].copy()
        df.columns = ["code", "name"]
    else:
        raise ValueError(f"股票列表字段异常：{stock_info.columns.tolist()}")

    df["code"] = df["code"].apply(normalize_code)
    df["name"] = df["name"].astype(str)
    df = df[~df["name"].str.contains(r"ST|退", case=False, regex=True, na=False)]
    df = df[df["code"].str.startswith(("60", "00"))]
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    print(f"主板股票数量：{len(df)}")
    return df


def download_all_stocks(
    history_days: int = 30,
    data_dir: str = "data",
    cache_valid_hours: int = 12,
    adjust: str = "qfq",
    max_workers: int = 8,
    sleep_on_error: float = 0.5,
):
    """下载所有主板股票的历史行情并缓存"""
    universe = get_mainboard_stocks()
    codes = universe["code"].tolist()
    success_count = 0
    fail_count = 0

    print(f"开始下载 {len(codes)} 只股票，使用 {max_workers} 个线程...")

    def _download(code):
        try:
            df = fetch_stock_history(
                code=code,
                history_days=history_days,
                data_dir=data_dir,
                cache_valid_hours=cache_valid_hours,
                adjust=adjust,
                min_bars=1,  # 下载时不强制最小bar数，由调用方决定
                sleep_on_error=sleep_on_error,
            )
            if df is not None and not df.empty:
                return True
            return False
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download, code): code for code in codes}
        for future in tqdm(as_completed(futures), total=len(futures)):
            try:
                ok = future.result()
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

    print(f"下载完成：成功 {success_count}，失败 {fail_count}")


# ==============================
# 命令行入口
# ==============================

def main():
    parser = argparse.ArgumentParser(
        description="A股历史日线数据下载器（主板）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  python stock_data.py --code 600000,000001
  python stock_data.py --all --days 30 --workers 8
        """
    )
    parser.add_argument(
        "--code",
        type=str,
        help="单个或多个股票代码，用逗号分隔，如 600000,000001"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="下载所有主板股票（约3000+只）"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="回溯天数（默认1天）"
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
        help="缓存目录（默认 data）"
    )
    parser.add_argument(
        "--cache-hours",
        type=int,
        default=3000,
        help="缓存有效小时数（默认3000），超过后重新下载"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="并发下载线程数（默认8）"
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="qfq",
        choices=["qfq", "hfq", ""],
        help="复权方式（qfq前复权，hfq后复权，''不复权）"
    )

    args = parser.parse_args()

    if not args.code and not args.all:
        parser.print_help()
        return

    if args.code:
        codes = [c.strip() for c in args.code.split(",") if c.strip()]
        for code in codes:
            print(f"正在下载 {code} ...")
            df = fetch_stock_history(
                code=code,
                history_days=args.days,
                data_dir=args.data_dir,
                cache_valid_hours=args.cache_hours,
                adjust=args.adjust,
                min_bars=1,
                sleep_on_error=0.5,
            )
            if df is not None and not df.empty:
                print(f"  {code} 下载成功，共 {len(df)} 条数据，文件已缓存至 {args.data_dir}/{code}.csv")
            else:
                print(f"  {code} 下载失败或无数据")

    elif args.all:
        download_all_stocks(
            history_days=args.days,
            data_dir=args.data_dir,
            cache_valid_hours=args.cache_hours,
            adjust=args.adjust,
            max_workers=args.workers,
            sleep_on_error=0.5,
        )

    print("任务完成。")


if __name__ == "__main__":
    main()