"""工作人员可读的文本报告与 JSON 导出。

文本报告刻意不提供“总体安置成功率”这样的单一数字；它分别列出
已确认、待确认、未满足（按原因和无障碍需求）以及容量缺口。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date
from enum import Enum

from .domain import (
    PlanResult,
    PriorAssignment,
    Scenario,
)


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value else "未知"


def render_capacity(result: PlanResult) -> list[str]:
    lines = ["## 各时段可用容量（单元·天）", ""]
    if not result.capacity:
        return lines + ["（没有可核算的日期范围。）", ""]
    header = (
        "| 时段 | 可运营 | 空闲 | 已确认占用 | 待确认占用 | 口头预留 | 维修 | 状态未知 | 停运 | "
        "无障碍可运营 | 无障碍空闲 | 空闲单元数 | 最长连续空闲(天) |"
    )
    lines.append(header)
    lines.append("|---" * 14 + "|")
    for period in result.capacity:
        lines.append(
            f"| {period.label} | {period.operational_unit_days} | {period.free_unit_days} | "
            f"{period.assigned_unit_days} | {period.pending_unit_days} | {period.held_unit_days} | "
            f"{period.maintenance_unit_days} | {period.unknown_unit_days} | "
            f"{period.shutdown_unit_days} | {period.accessible_operational_days} | "
            f"{period.accessible_free_days} | {period.free_units} | "
            f"{period.longest_free_run_days} |"
        )
    lines.append("")
    lines.append("说明：维修、状态未知、停运、口头预留都不计入“空闲”容量；")
    lines.append("“待确认占用”只是计划占位，不等于已安置。")
    lines.append("")
    lines.append("### 各时段需求与未安置")
    lines.append("")
    lines.append("| 时段 | 待安置家庭 | 其中无障碍 | 联系不上 | 其中无障碍 | 未安置(可行动) | 其中无障碍 |")
    lines.append("|---|---|---|---|---|---|---|")
    for period in result.capacity:
        lines.append(
            f"| {period.label} | {period.requesting_households} | "
            f"{period.requesting_step_free_households} | {period.unreachable_households} | "
            f"{period.unreachable_step_free_households} | {period.unplaced_active_households} | "
            f"{period.unplaced_step_free_households} |"
        )
    lines.append("")
    return lines


def render_assignments(result: PlanResult) -> list[str]:
    lines = ["## 已确认安排", ""]
    confirmed = result.confirmed_assignments
    if not confirmed:
        lines.append("（暂无已确认安排。）")
    else:
        lines.append("| 家庭 | 单元 | 入住 | 搬出 | 得分 | 保留自上次 | 确认依据 |")
        lines.append("|---|---|---|---|---|---|---|")
        for assignment in confirmed:
            lines.append(
                f"| {assignment.household_token} | {assignment.unit_id} | "
                f"{_fmt_date(assignment.start)} | {_fmt_date(assignment.end)} | "
                f"{assignment.score if assignment.score is not None else '—'} | "
                f"{'是' if assignment.retained else '否'} | {assignment.confirmation_evidence} |"
            )
    lines.append("")
    lines.append("## 待确认方案（未安置）")
    lines.append("")
    pending = result.pending_assignments
    if not pending:
        lines.append("（暂无待确认方案。）")
    else:
        lines.append("| 家庭 | 单元 | 入住 | 搬出 | 得分 | 确认状态 | 确认依据 |")
        lines.append("|---|---|---|---|---|---|---|")
        for assignment in pending:
            lines.append(
                f"| {assignment.household_token} | {assignment.unit_id} | "
                f"{_fmt_date(assignment.start)} | {_fmt_date(assignment.end)} | "
                f"{assignment.score if assignment.score is not None else '—'} | "
                f"{assignment.confirmation.label} | {assignment.confirmation_evidence} |"
            )
    lines.append("")
    return lines


def render_unmet(result: PlanResult) -> list[str]:
    lines = ["## 未满足需求（匿名类别）", ""]
    if not result.unmet:
        lines.append("（当前没有未满足的需求。）")
        lines.append("")
        return lines
    lines.append("| 家庭 | 确认状态 | 原因类别 | 无障碍需求 | 照护距离上限 | 搬迁窗口 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|")
    for need in result.unmet:
        lines.append(
            f"| {need.household_token} | {need.confirmation.label} | {need.category.label} | "
            f"{'需要' if need.needs_step_free else ('未知' if need.needs_step_free is None else '不需要')} | "
            f"{need.care_distance_limit if need.care_distance_limit is not None else '未知'} | "
            f"{_fmt_date(need.move_start)}–{_fmt_date(need.move_end)} | {need.summary} |"
        )
        if need.details:
            lines.append(f"|  |  |  |  |  |  | {'；'.join(need.details)} |")
        if need.potential_barriers:
            lines.append(
                f"|  |  |  |  |  |  | 若恢复联系可能遇到：{'；'.join(need.potential_barriers)} |"
            )
    lines.append("")
    return lines


def render_coverage(result: PlanResult) -> list[str]:
    lines = ["## 分类覆盖情况（不合并为总体成功率）", ""]
    lines.append("| 需求类别 | 登记 | 已确认需求 | 已确认安置 | 待确认方案 | 未服务(已确认) | "
                 "未服务(待协商) | 未服务(联系不上) | 未服务(拒绝) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in result.coverage:
        lines.append(
            f"| {row.need} | {row.demand_total} | {row.demand_confirmed} | "
            f"{row.placed_confirmed} | {row.placed_pending} | {row.unserved_confirmed} | "
            f"{row.unserved_pending} | {row.unserved_unreachable} | {row.unserved_declined} |"
        )
    lines.append("")
    return lines


def render_shortfalls(result: PlanResult) -> list[str]:
    lines = ["## 容量缺口（逐日核算）", ""]
    for shortfall in result.shortfalls:
        lines.append(f"- {shortfall.note}")
    lines.append("")
    return lines


def render_batches(result: PlanResult) -> list[str]:
    lines = ["## 施工批次可开始条件", ""]
    if not result.batches:
        return lines + ["（没有施工批次。）", ""]
    lines.append("| 批次 | 街区 | 计划开始 | 需搬迁户数 | 已确认 | 待确认 | 尚缺 | 可开始 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for batch in result.batches:
        reasons = "；".join(batch.reasons) or "；".join(batch.conditions) or "—"
        lines.append(
            f"| {batch.batch_id} | {batch.block} | {_fmt_date(batch.planned_start)} | "
            f"{batch.households_required if batch.households_required is not None else '未知'} | "
            f"{batch.confirmed_relocated} | {batch.pending_relocated} | "
            f"{batch.gap if batch.gap is not None else '未知'} | "
            f"{'是' if batch.can_start else '否'} | {reasons} |"
        )
    lines.append("")
    return lines


def render_tasks(result: PlanResult) -> list[str]:
    lines = ["## 后续联络与协调任务", ""]
    if not result.tasks:
        return lines + ["（没有生成任务。）", ""]
    lines.append("| 编号 | 优先级 | 类型 | 对象 | 负责人 | 期限 | 动作 | 原因 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for task in result.tasks:
        lines.append(
            f"| {task.task_id} | {task.priority.label} | {task.kind} | {task.subject} | "
            f"{task.owner_role} | {_fmt_date(task.due)} | {task.action} | {task.reason} |"
        )
    lines.append("")
    return lines


def render_service_groups(result: PlanResult) -> list[str]:
    lines = ["## 可共享接驳（不共用居住单元）", ""]
    if not result.service_groups:
        return lines + ["（暂无可合并的接驳需求。）", ""]
    for group in result.service_groups:
        lines.append(f"- {group.label}：{'、'.join(group.household_tokens)}；{group.note}")
    lines.append("")
    return lines


def render_released(result: PlanResult) -> list[str]:
    lines = ["## 重算时释放的旧安排", ""]
    if not result.released:
        return lines + ["（没有释放旧安排。）", ""]
    lines.append("| 家庭 | 单元 | 原入住 | 原搬出 | 释放原因 |")
    lines.append("|---|---|---|---|---|")
    for release in result.released:
        lines.append(
            f"| {release.household_token} | {release.unit_id} | {_fmt_date(release.start)} | "
            f"{_fmt_date(release.end)} | {release.reason} |"
        )
    lines.append("")
    return lines


def render_recommendations(result: PlanResult) -> list[str]:
    lines = ["## 需要调整的施工安排或新增服务", ""]
    if not result.recommendations:
        return lines + ["（当前没有需要升级处理的容量或批次问题。）", ""]
    lines += [f"- {item}" for item in result.recommendations]
    lines.append("")
    return lines


def render_issues(result: PlanResult) -> list[str]:
    lines = ["## 数据说明", ""]
    for warning in result.warnings:
        lines.append(f"- {warning}")
    for issue in result.issues:
        where = f"（{issue.source} 第 {issue.row} 行）" if issue.source and issue.row else (
            f"（{issue.source}）" if issue.source else ""
        )
        lines.append(f"- [{issue.severity}] {issue.message}{where}")
    lines.append("")
    return lines


def render_explanation(result: PlanResult, household_token: str,
                       evaluations: list) -> list[str]:
    lines = [f"## {household_token} 的匹配排序解释", ""]
    lines.append("| 排序 | 单元 | 可分配 | 得分 | 状态 | 阻塞/注意 |")
    lines.append("|---|---|---|---|---|---|")
    for evaluation in evaluations:
        notes = "；".join(evaluation.blockers) or "；".join(evaluation.negatives) or "—"
        lines.append(
            f"| {evaluation.rank} | {evaluation.unit_id} | {'是' if evaluation.eligible else '否'} | "
            f"{evaluation.score if evaluation.score is not None else '—'} | "
            f"{evaluation.status.label if evaluation.status else '—'} | {notes} |"
        )
        for check in evaluation.checks:
            lines.append(f"|  |  |  |  | {check.status.label} | {check.kind}: {check.detail} |")
    lines.append("")
    assignment = next((a for a in result.assignments if a.household_token == household_token), None)
    if assignment:
        lines.append(f"当前选中：{assignment.unit_id}。{assignment.explanation.summary}")
        if assignment.explanation.alternatives:
            lines.append("备选：" + "；".join(assignment.explanation.alternatives))
    else:
        lines.append("当前未选中任何单元；请查看未满足需求与后续任务。")
    lines.append("")
    return lines


def render_text(result: PlanResult, scenario: Scenario, *,
                evaluations_by_household: dict[str, list] | None = None) -> str:
    lines = [
        "# 老旧小区施工周转安置安排",
        "",
        f"生成日期（as_of）：{_fmt_date(result.generated_for)}",
        "",
        "本报告区分“已确认安排”“待确认方案”与“未满足需求”，不提供会掩盖",
        "无障碍缺口的单一安置成功率。",
        "",
    ]
    sections = [
        render_recommendations(result),
        render_shortfalls(result),
        render_capacity(result),
        render_assignments(result),
        render_unmet(result),
        render_coverage(result),
        render_batches(result),
        render_service_groups(result),
        render_released(result),
        render_tasks(result),
        render_issues(result),
    ]
    for section in sections:
        lines += section
    for token, evaluations in (evaluations_by_household or {}).items():
        lines += render_explanation(result, token, evaluations)
    return "\n".join(lines).rstrip() + "\n"


def _jsonable(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def render_json(result: PlanResult, scenario: Scenario,
                evaluations: dict[str, list] | None = None) -> str:
    payload = {
        "generated_for": _jsonable(result.generated_for),
        "assignments": _jsonable(result.assignments),
        "unmet": _jsonable(result.unmet),
        "capacity": _jsonable(result.capacity),
        "batches": _jsonable(result.batches),
        "tasks": _jsonable(result.tasks),
        "coverage": _jsonable(result.coverage),
        "service_groups": _jsonable(result.service_groups),
        "released": _jsonable(result.released),
        "shortfalls": _jsonable(result.shortfalls),
        "recommendations": list(result.recommendations),
        "warnings": list(result.warnings),
        "issues": _jsonable(result.issues),
        "batch_links": dict(result.batch_links),
        "unassigned_tokens": list(result.unassigned_tokens),
        "explanations": _jsonable(evaluations or {}),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_prior(path: str | None) -> list[PriorAssignment]:
    if not path:
        return []
    import csv

    with open(path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("历史安排文件缺少表头。")
        required = {"household_token", "unit_id", "start", "end"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"历史安排文件缺少字段：{', '.join(sorted(missing))}")
        return [PriorAssignment.from_dict(row) for row in reader]
