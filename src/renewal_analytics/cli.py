"""命令行入口：读取合成输入，输出可读报告与机器可读结果。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .loader import InputError, load_scenario
from .planner import PlanError, build_plan, explain_household
from .report import load_prior, render_json, render_text


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renewal-analytics",
        description="老旧小区施工周转住房的容量、匹配与未满足需求安排工具。",
    )
    parser.add_argument("--data", default="data", help="输入 CSV 所在目录（默认 data）")
    parser.add_argument("--as-of", type=_parse_date, default=date.today(),
                        help="确认状态与有效期的计算基准日（ISO 8601，默认今天）")
    parser.add_argument("--prior", default=None,
                        help="上一次安排 CSV（household_token,unit_id,start,end[,status]），用于重算")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--explain", action="append", default=[],
                        metavar="HOUSEHOLD", help="输出指定家庭的匹配排序解释，可重复")
    parser.add_argument("--output", default=None, help="写入文件而不是标准输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        scenario = load_scenario(Path(args.data))
        prior = load_prior(args.prior)
        result = build_plan(scenario, prior, as_of=args.as_of)
    except (InputError, PlanError, ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    evaluations: dict[str, list] = {}
    if args.format == "text":
        for token in args.explain:
            try:
                evaluations[token] = explain_household(
                    scenario, token, as_of=args.as_of, prior=prior,
                )
            except KeyError as error:
                print(f"错误：{error}", file=sys.stderr)
                return 2
        rendered = render_text(result, scenario, evaluations_by_household=evaluations)
    else:
        for token in args.explain:
            try:
                evaluations[token] = explain_household(
                    scenario, token, as_of=args.as_of, prior=prior,
                )
            except KeyError as error:
                print(f"错误：{error}", file=sys.stderr)
                return 2
        rendered = render_json(result, scenario, evaluations)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
