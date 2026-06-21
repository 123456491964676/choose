#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
批量运行 pool.py / pool02.py

功能：
1. 从基准文件 data/_benchmark_000300.csv 读取交易日；
2. 在指定日期区间内逐个交易日调用 pool 脚本；
3. 每个日期单独保存日志到 logs/pool_YYYY-MM-DD.txt；
4. 保存总日志到 logs/batch_起始_to_结束.txt；
5. 保存运行汇总到 logs/batch_summary_起始_to_结束.csv；
6. 默认过滤 tqdm 进度条，避免日志文件非常大；
7. 支持动态卖出参数。

示例：
    python run_pool_batch.py

    python run_pool_batch.py --start 2026-04-01 --end 2026-04-30 --script pool02.py

    python run_pool_batch.py \
        --start 2026-04-01 \
        --end 2026-04-30 \
        --script pool02.py \
        --days 30 \
        --universe-source local \
        --stop-loss -0.06 \
        --trailing-start 0.12 \
        --trailing-drawdown 0.07

如果想保留进度条日志：
    python run_pool_batch.py --keep-progress
"""

import os
import re
import sys
import time
import argparse
import subprocess
from pathlib import Path
from typing import List, Optional, Dict

import pandas as pd


# ==============================
# 默认配置
# ==============================

DEFAULT_START_DATE = "2026-04-01"
DEFAULT_END_DATE = "2026-04-30"

DEFAULT_POOL_SCRIPT = "pool02.py"
DEFAULT_BENCHMARK_FILE = "data/_benchmark_000300.csv"

DEFAULT_DAYS = 30
DEFAULT_UNIVERSE_SOURCE = "local"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_LOG_DIR = "logs"


# ==============================
# 工具函数
# ==============================

def find_date_column(df: pd.DataFrame) -> str:
    """
    自动识别日期列。
    """
    for c in ["date", "日期", "时间", "trade_date", "交易日期"]:
        if c in df.columns:
            return c

    raise ValueError(f"基准文件中找不到日期列，实际字段：{df.columns.tolist()}")


def load_trading_dates(
    benchmark_file: str,
    start_date: str,
    end_date: str,
) -> List[pd.Timestamp]:
    """
    从基准文件中读取指定区间内的交易日。
    """
    if not os.path.exists(benchmark_file):
        raise FileNotFoundError(f"基准文件不存在：{benchmark_file}")

    bench = pd.read_csv(benchmark_file)
    date_col = find_date_column(bench)

    bench[date_col] = pd.to_datetime(bench[date_col], errors="coerce").dt.normalize()
    bench = bench.dropna(subset=[date_col])

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()

    trading_dates = (
        bench.loc[
            (bench[date_col] >= start) & (bench[date_col] <= end),
            date_col,
        ]
        .drop_duplicates()
        .sort_values()
        .tolist()
    )

    return [pd.Timestamp(d).normalize() for d in trading_dates]


def split_control_line(line: str) -> List[str]:
    """
    tqdm 经常用 \\r 刷新同一行。
    重定向时会导致很多控制字符。
    这里把一行拆干净。
    """
    if line is None:
        return []

    s = str(line)

    # 去掉 ANSI 颜色控制符
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)

    # \r 变成换行，便于分段过滤
    s = s.replace("\r", "\n")

    return s.splitlines()


def is_progress_line(line: str) -> bool:
    """
    判断是否为 tqdm / AkShare 进度条行。
    """
    if line is None:
        return True

    s = str(line).strip()

    if not s:
        return False

    progress_keywords = [
        "Please wait for a moment",
        "股票数据读取:",
        "it/s]",
        "?it/s]",
        "%|",
        "████",
        "|          |",
        "|█",
        "|▏",
        "|▎",
        "|▍",
        "|▌",
        "|▋",
        "|▊",
        "|▉",
    ]

    for k in progress_keywords:
        if k in s:
            return True

    # tqdm 常见格式：
    # 12%|█▏        | 353/3028 [00:12<01:30, 29.48it/s]
    if re.search(r"\d+%\|.*\|.*it/s", s):
        return True

    return False


def write_line(f, line: str):
    f.write(line + "\n")
    f.flush()


def ensure_dir(path: str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def build_command(args, date_str: str) -> List[str]:
    """
    构建调用 pool 脚本的命令。
    """
    cmd = [
        sys.executable,
        args.script,
        "--date",
        date_str,
        "--days",
        str(args.days),
        "--universe-source",
        args.universe_source,
        "--output-dir",
        args.output_dir,
        "--cache-dir",
        args.cache_dir,
        "--min-bars",
        str(args.min_bars),
        "--min-score",
        str(args.min_score),
        "--min-amount",
        str(args.min_amount),
        "--buy-price-field",
        args.buy_price_field,
        "--stop-loss",
        str(args.stop_loss),
        "--trailing-start",
        str(args.trailing_start),
        "--trailing-drawdown",
        str(args.trailing_drawdown),
        "--min-hold-days",
        str(args.min_hold_days),
        "--exit-sell-timing",
        args.exit_sell_timing,
    ]

    if args.statuses:
        cmd.extend(["--statuses", args.statuses])

    if args.use_prev_trading_day:
        cmd.append("--use-prev-trading-day")

    if args.no_dynamic_exit:
        cmd.append("--no-dynamic-exit")

    return cmd


def run_one_date(
    args,
    date_str: str,
    log_dir: Path,
    batch_f,
) -> Dict:
    """
    运行单个交易日。
    """
    log_file = log_dir / f"pool_{date_str}.txt"
    cmd = build_command(args, date_str)

    start_time = time.time()

    batch_f.write(f"开始运行: {date_str}\n")
    batch_f.write(f"命令: {' '.join(cmd)}\n")
    batch_f.write(f"日志: {log_file}\n")
    batch_f.flush()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # 有些 tqdm 会识别这个环境变量，有些不会。
    # 不生效也没关系，后面会过滤。
    if not args.keep_progress:
        env["TQDM_DISABLE"] = "1"

    with log_file.open("w", encoding="utf-8") as f:
        write_line(f, f"运行日期: {date_str}")
        write_line(f, f"命令: {' '.join(cmd)}")
        write_line(f, "=" * 100)
        write_line(f, "")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        assert process.stdout is not None

        last_lines = []

        for raw_line in process.stdout:
            parts = split_control_line(raw_line)

            for part in parts:
                if not args.keep_progress and is_progress_line(part):
                    continue

                write_line(f, part)

                # 记录最近若干行，方便汇总失败原因
                if part.strip():
                    last_lines.append(part.strip())
                    if len(last_lines) > 30:
                        last_lines.pop(0)

        return_code = process.wait()

        elapsed = time.time() - start_time

        write_line(f, "")
        write_line(f, "=" * 100)
        write_line(f, f"运行日期: {date_str}")
        write_line(f, f"退出码: {return_code}")
        write_line(f, f"耗时: {format_seconds(elapsed)}")
        write_line(f, "=" * 100)

    status = "success" if return_code == 0 else "failed"

    # 简单提取失败原因
    error_hint = ""
    if return_code != 0:
        for line in reversed(last_lines):
            if any(k in line for k in ["Traceback", "Error", "错误", "Exception", "NameError", "ValueError"]):
                error_hint = line
                break
        if not error_hint and last_lines:
            error_hint = last_lines[-1]

    batch_f.write(f"完成: {date_str}, 状态: {status}, 退出码: {return_code}, 耗时: {format_seconds(elapsed)}\n")
    if error_hint:
        batch_f.write(f"失败提示: {error_hint}\n")
    batch_f.write("-" * 100 + "\n")
    batch_f.flush()

    return {
        "date": date_str,
        "status": status,
        "return_code": return_code,
        "elapsed_seconds": round(elapsed, 2),
        "elapsed_text": format_seconds(elapsed),
        "log_file": str(log_file),
        "command": " ".join(cmd),
        "error_hint": error_hint,
    }


# ==============================
# 命令行参数
# ==============================

def parse_args():
    parser = argparse.ArgumentParser(
        description="批量循环运行 pool.py / pool02.py"
    )

    parser.add_argument(
        "--start",
        type=str,
        default=DEFAULT_START_DATE,
        help=f"开始日期，默认 {DEFAULT_START_DATE}",
    )

    parser.add_argument(
        "--end",
        type=str,
        default=DEFAULT_END_DATE,
        help=f"结束日期，默认 {DEFAULT_END_DATE}",
    )

    parser.add_argument(
        "--script",
        type=str,
        default=DEFAULT_POOL_SCRIPT,
        help=f"要运行的脚本，默认 {DEFAULT_POOL_SCRIPT}",
    )

    parser.add_argument(
        "--benchmark-file",
        type=str,
        default=DEFAULT_BENCHMARK_FILE,
        help=f"基准文件，默认 {DEFAULT_BENCHMARK_FILE}",
    )

    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data",
        help="行情数据目录，默认 data",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help=f"pool 输出目录，默认 {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--log-dir",
        type=str,
        default=DEFAULT_LOG_DIR,
        help=f"日志目录，默认 {DEFAULT_LOG_DIR}",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"未来观察交易日数，默认 {DEFAULT_DAYS}",
    )

    parser.add_argument(
        "--universe-source",
        type=str,
        default=DEFAULT_UNIVERSE_SOURCE,
        choices=["local", "ak", "auto"],
        help=f"股票池来源，默认 {DEFAULT_UNIVERSE_SOURCE}",
    )

    parser.add_argument(
        "--statuses",
        type=str,
        default="强趋势池,趋势观察池",
        help="要分析的股票池，逗号分隔",
    )

    parser.add_argument(
        "--use-prev-trading-day",
        action="store_true",
        help="如果传入日期非交易日，让 pool 脚本自动使用前一交易日。当前批量脚本默认只跑交易日，一般不用开。",
    )

    parser.add_argument(
        "--min-bars",
        type=int,
        default=130,
        help="最少K线数量，默认 130",
    )

    parser.add_argument(
        "--min-score",
        type=int,
        default=75,
        help="趋势观察池最低评分，默认 75",
    )

    parser.add_argument(
        "--min-amount",
        type=float,
        default=80_000_000,
        help="20日平均成交额门槛，默认 80000000",
    )

    parser.add_argument(
        "--buy-price-field",
        type=str,
        default="open",
        choices=["open", "close"],
        help="买入价格字段，默认 open",
    )

    # 动态卖出参数
    parser.add_argument(
        "--no-dynamic-exit",
        action="store_true",
        help="关闭动态卖出。默认不开这个参数，即动态卖出开启。",
    )

    parser.add_argument(
        "--min-hold-days",
        type=int,
        default=3,
        help="动态卖出最少持有天数，默认 3",
    )

    parser.add_argument(
        "--exit-sell-timing",
        type=str,
        default="next_open",
        choices=["close", "next_open"],
        help="动态卖出执行时点，默认 next_open",
    )

    parser.add_argument(
        "--stop-loss",
        type=float,
        default=-0.08,
        help="硬止损比例，默认 -0.08",
    )

    parser.add_argument(
        "--trailing-start",
        type=float,
        default=0.10,
        help="移动止盈启动收益，默认 0.10",
    )

    parser.add_argument(
        "--trailing-drawdown",
        type=float,
        default=0.08,
        help="移动止盈回撤比例，默认 0.08",
    )

    # 批处理控制
    parser.add_argument(
        "--keep-progress",
        action="store_true",
        help="保留 tqdm 进度条日志。默认过滤。",
    )

    parser.add_argument(
        "--skip-existing-log",
        action="store_true",
        help="如果某日期日志已存在，则跳过该日期。",
    )

    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="一旦某天运行失败，就停止整个批次。默认失败也继续。",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要运行的命令，不实际运行。",
    )

    return parser.parse_args()


# ==============================
# 主函数
# ==============================

def main():
    args = parse_args()

    log_dir = ensure_dir(args.log_dir)
    ensure_dir(args.output_dir)

    if not os.path.exists(args.script):
        raise FileNotFoundError(f"找不到要运行的脚本：{args.script}")

    trading_dates = load_trading_dates(
        benchmark_file=args.benchmark_file,
        start_date=args.start,
        end_date=args.end,
    )

    if not trading_dates:
        print(f"区间内没有交易日：{args.start} ~ {args.end}")
        return

    batch_log = log_dir / f"batch_{args.start}_to_{args.end}.txt"
    summary_csv = log_dir / f"batch_summary_{args.start}_to_{args.end}.csv"

    records = []

    total_start = time.time()

    with batch_log.open("w", encoding="utf-8") as batch_f:
        batch_f.write("批量运行 pool 脚本\n")
        batch_f.write("=" * 100 + "\n")
        batch_f.write(f"日期区间: {args.start} ~ {args.end}\n")
        batch_f.write(f"交易日数量: {len(trading_dates)}\n")
        batch_f.write(f"脚本: {args.script}\n")
        batch_f.write(f"基准文件: {args.benchmark_file}\n")
        batch_f.write(f"数据目录: {args.cache_dir}\n")
        batch_f.write(f"输出目录: {args.output_dir}\n")
        batch_f.write(f"日志目录: {args.log_dir}\n")
        batch_f.write(f"days: {args.days}\n")
        batch_f.write(f"universe_source: {args.universe_source}\n")
        batch_f.write(f"statuses: {args.statuses}\n")
        batch_f.write(f"动态卖出: {'关闭' if args.no_dynamic_exit else '开启'}\n")
        batch_f.write(f"min_hold_days: {args.min_hold_days}\n")
        batch_f.write(f"exit_sell_timing: {args.exit_sell_timing}\n")
        batch_f.write(f"stop_loss: {args.stop_loss}\n")
        batch_f.write(f"trailing_start: {args.trailing_start}\n")
        batch_f.write(f"trailing_drawdown: {args.trailing_drawdown}\n")
        batch_f.write(f"keep_progress: {args.keep_progress}\n")
        batch_f.write("=" * 100 + "\n\n")
        batch_f.flush()

        print(f"批量运行开始：{args.start} ~ {args.end}")
        print(f"交易日数量：{len(trading_dates)}")
        print(f"总日志：{batch_log}")
        print(f"汇总CSV：{summary_csv}")

        for idx, dt in enumerate(trading_dates, start=1):
            date_str = pd.Timestamp(dt).strftime("%Y-%m-%d")
            log_file = log_dir / f"pool_{date_str}.txt"

            print(f"[{idx}/{len(trading_dates)}] 运行 {date_str} ...")

            if args.skip_existing_log and log_file.exists():
                print(f"  跳过，日志已存在：{log_file}")

                record = {
                    "date": date_str,
                    "status": "skipped",
                    "return_code": np.nan,
                    "elapsed_seconds": 0,
                    "elapsed_text": "0秒",
                    "log_file": str(log_file),
                    "command": " ".join(build_command(args, date_str)),
                    "error_hint": "log exists",
                }

                records.append(record)
                pd.DataFrame(records).to_csv(summary_csv, index=False, encoding="utf-8-sig")
                continue

            if args.dry_run:
                cmd = build_command(args, date_str)
                print("  DRY RUN:", " ".join(cmd))

                record = {
                    "date": date_str,
                    "status": "dry_run",
                    "return_code": np.nan,
                    "elapsed_seconds": 0,
                    "elapsed_text": "0秒",
                    "log_file": str(log_file),
                    "command": " ".join(cmd),
                    "error_hint": "",
                }

                records.append(record)
                continue

            record = run_one_date(
                args=args,
                date_str=date_str,
                log_dir=log_dir,
                batch_f=batch_f,
            )

            records.append(record)

            # 每跑完一天就更新汇总，防止中途中断丢进度
            pd.DataFrame(records).to_csv(summary_csv, index=False, encoding="utf-8-sig")

            if args.stop_on_error and record["status"] == "failed":
                print(f"检测到失败，停止批处理：{date_str}")
                break

        total_elapsed = time.time() - total_start

        success_count = sum(1 for r in records if r["status"] == "success")
        failed_count = sum(1 for r in records if r["status"] == "failed")
        skipped_count = sum(1 for r in records if r["status"] == "skipped")
        dry_count = sum(1 for r in records if r["status"] == "dry_run")

        batch_f.write("\n")
        batch_f.write("=" * 100 + "\n")
        batch_f.write("批量运行结束\n")
        batch_f.write(f"总耗时: {format_seconds(total_elapsed)}\n")
        batch_f.write(f"成功: {success_count}\n")
        batch_f.write(f"失败: {failed_count}\n")
        batch_f.write(f"跳过: {skipped_count}\n")
        batch_f.write(f"DryRun: {dry_count}\n")
        batch_f.write(f"汇总CSV: {summary_csv}\n")
        batch_f.write("=" * 100 + "\n")

    pd.DataFrame(records).to_csv(summary_csv, index=False, encoding="utf-8-sig")

    print("\n批量运行完成")
    print(f"成功: {success_count}")
    print(f"失败: {failed_count}")
    print(f"跳过: {skipped_count}")
    print(f"总日志: {batch_log}")
    print(f"汇总CSV: {summary_csv}")


if __name__ == "__main__":
    main()