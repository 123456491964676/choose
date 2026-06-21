#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import subprocess
from pathlib import Path

import pandas as pd


START_DATE = "2026-04-01"
END_DATE = "2026-04-30"

POOL_SCRIPT = "pool.py"
BENCHMARK_FILE = "data/_benchmark_000300.csv"

DAYS = 30
UNIVERSE_SOURCE = "local"

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def find_date_column(df: pd.DataFrame) -> str:
    for c in ["date", "日期", "时间"]:
        if c in df.columns:
            return c
    raise ValueError(f"基准文件中找不到日期列，实际字段：{df.columns.tolist()}")


def main():
    bench = pd.read_csv(BENCHMARK_FILE)
    date_col = find_date_column(bench)

    bench[date_col] = pd.to_datetime(bench[date_col], errors="coerce").dt.normalize()
    bench = bench.dropna(subset=[date_col])

    start = pd.Timestamp(START_DATE).normalize()
    end = pd.Timestamp(END_DATE).normalize()

    trading_dates = (
        bench.loc[
            (bench[date_col] >= start) & (bench[date_col] <= end),
            date_col,
        ]
        .drop_duplicates()
        .sort_values()
        .tolist()
    )

    batch_log = LOG_DIR / f"batch_{START_DATE}_to_{END_DATE}.txt"

    with batch_log.open("w", encoding="utf-8") as batch_f:
        batch_f.write(f"批量运行区间: {START_DATE} ~ {END_DATE}\n")
        batch_f.write(f"交易日数量: {len(trading_dates)}\n")
        batch_f.write(f"UNIVERSE_SOURCE: {UNIVERSE_SOURCE}\n")
        batch_f.write("=" * 80 + "\n\n")

        for dt in trading_dates:
            date_str = pd.Timestamp(dt).strftime("%Y-%m-%d")
            log_file = LOG_DIR / f"pool_{date_str}.txt"

            cmd = [
                sys.executable,
                POOL_SCRIPT,
                "--date",
                date_str,
                "--days",
                str(DAYS),
                "--universe-source",
                UNIVERSE_SOURCE,
            ]

            batch_f.write(f"开始运行: {date_str}\n")
            batch_f.write(f"命令: {' '.join(cmd)}\n")
            batch_f.write(f"日志: {log_file}\n")
            batch_f.flush()

            with log_file.open("w", encoding="utf-8") as f:
                f.write(f"运行日期: {date_str}\n")
                f.write(f"命令: {' '.join(cmd)}\n")
                f.write("=" * 80 + "\n\n")
                f.flush()

                result = subprocess.run(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                f.write("\n")
                f.write("=" * 80 + "\n")
                f.write(f"运行日期: {date_str}\n")
                f.write(f"退出码: {result.returncode}\n")
                f.write("=" * 80 + "\n")

            batch_f.write(f"完成: {date_str}, 退出码: {result.returncode}\n")
            batch_f.write("-" * 80 + "\n")
            batch_f.flush()


if __name__ == "__main__":
    main()