#!/usr/bin/env python
"""清空历史字段画像中的样本值（本地维护脚本）。

背景：M1 修复轮加强了样本脱敏（值格式检测与保守遮蔽），但已持久化的历史画像
不会因代码更新自动变安全。本脚本只清空历史 `sample_values` 并标记需要重新画像，
保留元数据与统计指标；不连接任何数据源、不自动重扫、绝不打印样本内容。

用法（项目根目录、激活 nl2sql 环境）：
  python scripts/clear_profile_samples.py --db sqlite:///./metadata.db           # 预览（默认）
  python scripts/clear_profile_samples.py --db sqlite:///./metadata.db --apply   # 执行清理

清理后如需恢复样本，重新执行一次扫描即可（会按新规则重新画像）。
"""

import argparse
import json
import sqlite3
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清空历史画像样本值（不打印任何样本内容）")
    parser.add_argument(
        "--db",
        default="sqlite:///./metadata.db",
        help="元数据库 URL，当前仅支持 SQLite（默认 sqlite:///./metadata.db）",
    )
    parser.add_argument("--apply", action="store_true", help="实际执行清理；缺省为只读预览")
    return parser.parse_args()


def sqlite_path(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"仅支持 SQLite URL（sqlite:///路径），收到：{url.split('@')[0]}")
    return url[len(prefix):] or ":memory:"


def main() -> int:
    args = parse_args()
    path = sqlite_path(args.db)
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT rowid, payload FROM column_profiles").fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"无法读取 column_profiles 表（元数据库未初始化？）：{exc}") from exc

    total = with_samples = invalid = 0
    updates: list[tuple[str, int]] = []
    for rowid, payload in rows:
        total += 1
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            invalid += 1
            continue
        if not isinstance(data, dict) or not data.get("sample_values"):
            continue
        with_samples += 1
        data["sample_values"] = []
        data["samples_redacted"] = True
        data["sample_values_cleared"] = True
        data["cleared_reason"] = "masking-rules-upgraded-reprofile-required"
        updates.append((json.dumps(data, ensure_ascii=False), rowid))

    mode = "已执行" if args.apply else "预览（未写入，加 --apply 执行）"
    print(f"模式：{mode}")
    print(f"画像总数：{total}；含样本值：{with_samples}；载荷损坏跳过：{invalid}")
    if args.apply and updates:
        with connection:
            connection.executemany(
                "UPDATE column_profiles SET payload = ? WHERE rowid = ?", updates
            )
        print(f"已清空样本值并标记需重新画像：{len(updates)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
