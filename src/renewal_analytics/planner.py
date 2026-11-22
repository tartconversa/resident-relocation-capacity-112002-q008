"""可解释的周转住房分配、容量核算与施工批次联动。

设计原则
--------
1. 未知不等于满足：空的维护状态、无障碍字段、可用日期都不会被当作
   “可入住”“不需要无障碍”或“无限期可用”。
2. 一个单元同一时间只属于一个家庭：所有分配按 [开始, 结束] 闭区间
   占用，重算时先落实仍有效的已确认安排，再分配其余家庭。
3. 没有超额分配：容量不足只产生未满足需求、缺口与调整建议。
4. 无障碍需求单独核算：先分配有无障碍需求的家庭，普通家庭最后才
   会动用无障碍单元。
5. 每个“家庭 × 单元”都有可复核的得分与阻塞原因。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping

from .domain import (
    Assignment,
    AssignmentStatus,
    BatchState,
    CandidateEvaluation,
    Check,
    CheckStatus,
    Confirmation,
    ConfirmationState,
    ContactEntry,
    Explanation,
    Household,
    MaintenanceStatus,
    NeedCoverage,
    Outcome,
    PeriodCapacity,
    PlanResult,
    PriorAssignment,
    ReleasedAssignment,
    Scenario,
    ServiceGroup,
    Shortfall,
    Task,
    TaskPriority,
    Unit,
    UnmetCategory,
    UnmetNeed,
    assignment_sort_key,
    contact_sort_key,
)

# 已接受确认在没有显式 valid_to 时的默认有效期。
DEFAULT_ACCEPTED_VALID_DAYS = 120
# “要求替代方案”的默认有效期。
DEFAULT_NEGOTIATION_VALID_DAYS = 60
# 依赖批次完成后默认再等待的天数（缺少真实完工日期时的可解释假设）。
DEFAULT_DEPENDENCY_LEAD_DAYS = 30

CONFIRMATION_RANK = {
    Confirmation.CONFIRMED: 0,
    Confirmation.NEGOTIATING: 1,
    Confirmation.UNKNOWN: 2,
    Confirmation.UNREACHABLE: 3,
    Confirmation.DECLINED: 4,
}

BLOCK_LABELS = {
    "not_actionable": "家庭明确拒绝或联系不上，暂不占用房源",
    "maintenance": "单元维修中",
    "unit_status_unknown": "单元维护状态未知",
    "unit_dates_unknown": "单元可用日期未知",
    "household_dates_unknown": "家庭搬迁日期未知",
    "date_window": "单元可用期未覆盖完整搬迁期",
    "shutdown": "单元在搬迁期内停运",
    "held": "单元已被口头预留",
    "occupied": "单元在该时段已被安排",
    "step_unit_unknown": "单元无障碍条件未知",
    "step_fail": "单元非无台阶，无法满足无障碍需求",
    "distance_unit_unknown": "单元服务距离未知",
    "distance_fail": "单元超出照护服务距离上限",
}

BLOCK_CATEGORY = {
    "not_actionable": UnmetCategory.DECLINED,
    "maintenance": UnmetCategory.MAINTENANCE,
    "unit_status_unknown": UnmetCategory.DATA_INCOMPLETE,
    "unit_dates_unknown": UnmetCategory.DATA_INCOMPLETE,
    "household_dates_unknown": UnmetCategory.DATA_INCOMPLETE,
    "date_window": UnmetCategory.DATE_WINDOW,
    "shutdown": UnmetCategory.SHUTDOWN,
    "held": UnmetCategory.HELD,
    "occupied": UnmetCategory.CAPACITY_EXHAUSTED,
    "step_unit_unknown": UnmetCategory.DATA_INCOMPLETE,
    "step_fail": UnmetCategory.ACCESSIBLE_UNAVAILABLE,
    "distance_unit_unknown": UnmetCategory.DATA_INCOMPLETE,
    "distance_fail": UnmetCategory.CARE_DISTANCE,
}

PRIORITY_ORDER = {TaskPriority.HIGH: 0, TaskPriority.MEDIUM: 1, TaskPriority.LOW: 2}


class PlanError(Exception):
    """无法安全生成计划的结构性错误。"""


# ---------------------------------------------------------------------------
# 日期与区间工具
# ---------------------------------------------------------------------------


def overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


def days_between(start: date, end: date) -> int:
    return (end - start).days + 1


def iter_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def month_periods(start: date, end: date) -> list[tuple[date, date]]:
    periods: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            following = date(cursor.year + 1, 1, 1)
        else:
            following = date(cursor.year, cursor.month + 1, 1)
        period_end = min(following - timedelta(days=1), end)
        periods.append((max(cursor, start), period_end))
        cursor = following
    return periods


# ---------------------------------------------------------------------------
# 确认状态解析
# ---------------------------------------------------------------------------


def _contact_expiry(contact: ContactEntry) -> date | None:
    if contact.valid_to is not None:
        return contact.valid_to
    if contact.contacted_at is None:
        return None
    if contact.outcome is Outcome.ACCEPTED:
        return contact.contacted_at + timedelta(days=DEFAULT_ACCEPTED_VALID_DAYS)
    if contact.outcome is Outcome.ALTERNATIVE_REQUESTED:
        return contact.contacted_at + timedelta(days=DEFAULT_NEGOTIATION_VALID_DAYS)
    # 拒绝、联系不上、无人接听等没有自动失效期：需要人工重新接触。
    return None


def _outcome_to_confirmation(outcome: Outcome) -> Confirmation:
    return {
        Outcome.ACCEPTED: Confirmation.CONFIRMED,
        Outcome.DECLINED: Confirmation.DECLINED,
        Outcome.UNREACHABLE: Confirmation.UNREACHABLE,
        Outcome.NO_ANSWER: Confirmation.UNREACHABLE,
        Outcome.ALTERNATIVE_REQUESTED: Confirmation.NEGOTIATING,
        Outcome.UNKNOWN: Confirmation.UNKNOWN,
    }[outcome]


def resolve_confirmation(household: Household, contacts: Iterable[ContactEntry],
                         as_of: date) -> ConfirmationState:
    """结合有效联络与家庭表修订时间，给出可用于规划的确认状态。"""

    entries = sorted(contacts, key=contact_sort_key)
    relevant = [c for c in entries if c.contacted_at is None or c.contacted_at <= as_of]
    if not relevant:
        return ConfirmationState(
            household=household.confirmation,
            effective=household.confirmation,
            evidence="家庭表确认状态（无联络记录）",
        )

    valid: list[tuple[ContactEntry, date | None]] = []
    for contact in relevant:
        # 没有联络日期、也没有显式有效期的“接受”无法核验是否已失效，
        # 不能直接当作仍然有效。
        if contact.contacted_at is None and contact.valid_from is None and contact.valid_to is None:
            continue
        expiry = _contact_expiry(contact)
        if contact.valid_from is not None and contact.valid_from > as_of:
            continue
        if expiry is not None and expiry < as_of:
            continue
        valid.append((contact, expiry))

    latest_contact = relevant[-1]
    if (latest_contact.contacted_at is None
            and latest_contact.valid_from is None
            and latest_contact.valid_to is None):
        return ConfirmationState(
            household=household.confirmation,
            effective=Confirmation.UNKNOWN,
            evidence=f"最后一次联络 {latest_contact.contact_id} 缺少日期与有效期，无法核验",
            contact_id=latest_contact.contact_id,
            latest_contact=latest_contact,
        )
    household_revision_is_newer = (
        household.revised_at is not None
        and latest_contact.contacted_at is not None
        and household.revised_at > latest_contact.contacted_at
    )
    if household_revision_is_newer:
        # 修订搬迁日期或需求会改变安排内容；修订前的接受不能沿用。
        return ConfirmationState(
            household=household.confirmation,
            effective=Confirmation.UNKNOWN,
            evidence=(
                f"家庭表修订于 {household.revised_at.isoformat()}，"
                f"晚于最后一次联络 {latest_contact.contacted_at.isoformat()}；"
                "需按新日期/需求重新确认"
            ),
            contact_id=latest_contact.contact_id,
            latest_contact=latest_contact,
        )

    if not valid:
        latest_expiry = _contact_expiry(latest_contact)
        expiry_text = latest_expiry.isoformat() if latest_expiry else "未知日期"
        # 已接受的确认过期后必须重新确认；协商中/无人接听的窗口过期后
        # 仍然是“待协商/联系不上”，不能被错误地当成已确认。
        if latest_contact.outcome is Outcome.ACCEPTED:
            effective = Confirmation.UNKNOWN
        else:
            effective = _outcome_to_confirmation(latest_contact.outcome)
        return ConfirmationState(
            household=household.confirmation,
            effective=effective,
            evidence=(
                f"最后一次联络 {latest_contact.contact_id} 的确认已于 {expiry_text} 失效，"
                "需要重新确认"
            ),
            contact_id=latest_contact.contact_id,
            expires_on=latest_expiry,
            latest_contact=latest_contact,
        )

    contact, expiry = valid[-1]
    effective = _outcome_to_confirmation(contact.outcome)
    evidence = f"联络 {contact.contact_id}（{contact.outcome.label}"
    if expiry is not None:
        evidence += f"，有效至 {expiry.isoformat()}"
    evidence += "）"
    return ConfirmationState(
        household=household.confirmation,
        effective=effective,
        evidence=evidence,
        contact_id=contact.contact_id,
        expires_on=expiry,
        latest_contact=contact,
    )


# ---------------------------------------------------------------------------
# 单元状态
# ---------------------------------------------------------------------------


def _unit_operational_problem(unit: Unit, start: date, end: date,
                              shutdowns: tuple[tuple[date, date], ...]) -> tuple[str, str] | None:
    if unit.maintenance_status is MaintenanceStatus.MAINTENANCE:
        return "maintenance", BLOCK_LABELS["maintenance"]
    if unit.maintenance_status is not MaintenanceStatus.READY:
        return "unit_status_unknown", BLOCK_LABELS["unit_status_unknown"]
    if unit.available_from is None or unit.available_to is None:
        return "unit_dates_unknown", BLOCK_LABELS["unit_dates_unknown"]
    if unit.available_from > start or unit.available_to < end:
        return "date_window", (
            f"{BLOCK_LABELS['date_window']}：单元 {unit.available_from.isoformat()}"
            f"–{unit.available_to.isoformat()}，家庭 {start.isoformat()}–{end.isoformat()}"
        )
    for shut_start, shut_end in shutdowns:
        if overlaps(start, end, shut_start, shut_end):
            return "shutdown", (
                f"{BLOCK_LABELS['shutdown']}：{shut_start.isoformat()}–{shut_end.isoformat()}"
            )
    return None


def _unit_day_state(unit: Unit, day: date, shutdowns: tuple[tuple[date, date], ...]) -> str:
    """返回某单元某天在“可用期之内”的状态。

    先判断单元是否处于其可用期内；不在可用期就不是容量，也不再计入
    维修/预留天数。这样 2026-11 不会因为 U-3 计划 2027-02 起可用而
    被误报为维修容量。
    """

    if unit.available_from is None or unit.available_to is None:
        return "unknown"
    if not (unit.available_from <= day <= unit.available_to):
        return "unavailable"
    if any(start <= day <= end for start, end in shutdowns):
        return "shutdown"
    if unit.maintenance_status is MaintenanceStatus.MAINTENANCE:
        return "maintenance"
    if unit.maintenance_status is not MaintenanceStatus.READY:
        return "unknown"
    if unit.held_for:
        return "held"
    return "operational"


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


@dataclass
class _Evaluation:
    unit: Unit
    eligible: bool
    score: int | None
    status: AssignmentStatus | None
    checks: tuple[Check, ...]
    blockers: tuple[str, ...]
    positives: tuple[str, ...]
    negatives: tuple[str, ...]


def _evaluate_pair(household: Household, unit: Unit, confirmation: ConfirmationState,
                   occupied: Mapping[str, list[tuple[date, date, str]]],
                   shutdowns: tuple[tuple[date, date], ...]) -> _Evaluation:
    checks: list[Check] = []
    blockers: list[str] = []
    positives: list[str] = []
    negatives: list[str] = []

    def check(kind: str, status: CheckStatus, detail: str) -> None:
        checks.append(Check(kind=kind, status=status, detail=detail))

    if not confirmation.effective.is_actionable:
        blockers.append("not_actionable")
        check("confirmation", CheckStatus.FAIL, confirmation.effective.label)
    else:
        check("confirmation", CheckStatus.PASS, confirmation.evidence)
    expiry_before_move = (
        confirmation.expires_on is not None
        and household.move_start is not None
        and confirmation.expires_on < household.move_start
    )
    if expiry_before_move:
        check("confirmation_expiry", CheckStatus.UNKNOWN,
              f"确认有效至 {confirmation.expires_on.isoformat()}，早于入住日 {household.move_start.isoformat()}；"
              "入住前需重新确认")
    elif confirmation.expires_on is not None:
        check("confirmation_expiry", CheckStatus.PASS,
              f"确认有效至 {confirmation.expires_on.isoformat()}")

    if household.move_start is None or household.move_end is None:
        blockers.append("household_dates_unknown")
        check("dates", CheckStatus.UNKNOWN, BLOCK_LABELS["household_dates_unknown"])
        start = end = None
    else:
        start, end = household.move_start, household.move_end
        check("dates", CheckStatus.PASS, f"搬迁期 {start.isoformat()}–{end.isoformat()}")

    problem = _unit_operational_problem(unit, start, end, shutdowns) if start and end else None
    if problem:
        blockers.append(problem[0])
        check("unit_window", CheckStatus.FAIL, problem[1])
    elif start and end:
        check("unit_window", CheckStatus.PASS, (
            f"单元可用期 {unit.available_from.isoformat()}–{unit.available_to.isoformat()} 覆盖搬迁期"
        ))

    if unit.held_for:
        if unit.held_for == household.household_token:
            positives.append(f"单元已为 {household.household_token} 口头预留（仍需书面确认）")
            check("hold", CheckStatus.UNKNOWN, "口头预留不等于确认，仍需核实")
        else:
            blockers.append("held")
            check("hold", CheckStatus.FAIL, f"{BLOCK_LABELS['held']}（{unit.held_for}）")
    else:
        check("hold", CheckStatus.PASS, "无口头预留")

    if household.needs_step_free:
        if unit.step_free is True:
            check("step_free", CheckStatus.PASS, "单元无台阶，满足无障碍需求")
            positives.append("无台阶单元，满足无障碍需求")
        elif unit.step_free is False:
            blockers.append("step_fail")
            check("step_free", CheckStatus.FAIL, BLOCK_LABELS["step_fail"])
        else:
            blockers.append("step_unit_unknown")
            check("step_free", CheckStatus.UNKNOWN, BLOCK_LABELS["step_unit_unknown"])
    elif household.step_free is None:
        check("step_free", CheckStatus.UNKNOWN, "家庭无障碍需求未知，需核实")
        negatives.append("家庭无障碍需求未知，不能据此确认最终安排")
    else:
        check("step_free", CheckStatus.PASS, "家庭无无障碍需求")

    if household.care_distance_limit is not None:
        if unit.service_distance is None:
            blockers.append("distance_unit_unknown")
            check("care_distance", CheckStatus.UNKNOWN, BLOCK_LABELS["distance_unit_unknown"])
        elif unit.service_distance > household.care_distance_limit:
            blockers.append("distance_fail")
            check("care_distance", CheckStatus.FAIL, (
                f"{BLOCK_LABELS['distance_fail']}：{unit.service_distance:g} > "
                f"{household.care_distance_limit:g}"
            ))
        else:
            margin = household.care_distance_limit - unit.service_distance
            check("care_distance", CheckStatus.PASS, (
                f"服务距离 {unit.service_distance:g}，上限 {household.care_distance_limit:g}，余量 {margin:g}"
            ))
            positives.append(f"照护服务距离余量 {margin:g}")
    else:
        check("care_distance", CheckStatus.UNKNOWN, "家庭照护距离上限未知，需核实")
        negatives.append("家庭照护距离上限未知，不能据此确认最终安排")

    if start and end:
        conflicts = [window for window in occupied.get(unit.unit_id, ())
                     if overlaps(start, end, window[0], window[1])]
        if conflicts:
            blockers.append("occupied")
            check("occupancy", CheckStatus.FAIL, (
                f"该时段已有 {len(conflicts)} 个安排占用单元 {unit.unit_id}"
            ))
        else:
            check("occupancy", CheckStatus.PASS, "该时段单元空闲")

    # ---- 评分：仅对可提议的组合计算 ----
    score: int | None = None
    status: AssignmentStatus | None = None
    hard_blockers = [code for code in blockers if code != "not_actionable"]
    if not hard_blockers and start and end:
        score = 50
        if household.needs_step_free:
            score += 25
        if household.care_distance_limit is not None and unit.service_distance is not None:
            margin = household.care_distance_limit - unit.service_distance
            score += min(20, max(0, int(margin) * 2))
        elif unit.service_distance is not None:
            score += max(0, 10 - int(unit.service_distance))
        if unit.available_from is not None:
            slack = max(0, (start - unit.available_from).days)
            score += max(0, 10 - slack // 7)
            if slack <= 3:
                positives.append(f"可入住日期仅早于搬迁 {slack} 天")
        if household.block and unit.block and household.block == unit.block:
            score += 10
            positives.append("与家庭所在街区一致")
        if unit.held_for == household.household_token:
            score += 5
        if unit.step_free is True and not household.needs_step_free:
            score -= 8
            negatives.append("优先保留无障碍单元，非必要使用扣分")

        all_known = all(check.status is not CheckStatus.UNKNOWN for check in checks)
        if confirmation.effective is Confirmation.CONFIRMED and all_known and not expiry_before_move:
            status = AssignmentStatus.CONFIRMED
        else:
            status = AssignmentStatus.PENDING
            if confirmation.effective is not Confirmation.CONFIRMED:
                negatives.append(f"确认状态为“{confirmation.effective.label}”，只能作为待确认方案")
            if expiry_before_move:
                negatives.append("确认在入住前失效，只能作为待确认方案")
            if not all_known:
                negatives.append("存在未知字段，不能算作已确认安排")

    return _Evaluation(
        unit=unit,
        eligible=not hard_blockers and score is not None,
        score=score,
        status=status,
        checks=tuple(checks),
        blockers=tuple(dict.fromkeys(blockers)),
        positives=tuple(dict.fromkeys(positives)),
        negatives=tuple(dict.fromkeys(negatives)),
    )


def _candidate_rank_key(evaluation: _Evaluation) -> tuple:
    if evaluation.eligible:
        return (0, -(evaluation.score or 0), evaluation.unit.unit_id)
    return (1, 0, evaluation.unit.unit_id)


# ---------------------------------------------------------------------------
# 分配主流程
# ---------------------------------------------------------------------------


def _planning_horizon(scenario: Scenario, as_of: date) -> tuple[date, date]:
    """容量与缺口的时间范围。

    只取输入数据涉及的日期；``as_of`` 不参与，避免把与项目无关的
    月份显示成零容量。完全没有日期时退回 ``as_of``。
    """

    dates: list[date] = []
    for household in scenario.households.values():
        dates.extend(d for d in (household.move_start, household.move_end) if d)
    for unit in scenario.units.values():
        dates.extend(d for d in (unit.available_from, unit.available_to) if d)
        for start, end in scenario.shutdowns_for(unit.unit_id):
            dates.extend((start, end))
    for batch in scenario.batches.values():
        if batch.planned_start:
            dates.append(batch.planned_start)
    if not dates:
        return as_of, as_of
    return min(dates), max(dates)


def _batch_links(scenario: Scenario) -> dict[str, str]:
    """家庭 → 施工批次。优先显式字段，其次街区，最后“计划开始落在搬迁期内”。"""

    links: dict[str, str] = {}
    for token, household in scenario.households.items():
        if household.batch_id and household.batch_id in scenario.batches:
            links[token] = household.batch_id
            continue
        block_matches = [
            batch for batch in scenario.batches.values()
            if household.block and batch.block == household.block
        ]
        if len(block_matches) == 1:
            links[token] = block_matches[0].batch_id
            continue
        if household.move_start and household.move_end:
            window_matches = [
                batch for batch in scenario.batches.values()
                if batch.planned_start
                and household.move_start <= batch.planned_start <= household.move_end
            ]
            if window_matches:
                links[token] = min(
                    window_matches,
                    key=lambda b: (b.planned_start or date.max, b.batch_id),
                ).batch_id
    return links


def build_plan(scenario: Scenario, prior: Iterable[PriorAssignment] = (), *,
               as_of: date | None = None,
               dependency_lead_days: int = DEFAULT_DEPENDENCY_LEAD_DAYS) -> PlanResult:
    as_of = as_of or date.today()
    if dependency_lead_days < 0:
        raise ValueError("dependency_lead_days 不能为负。")
    issues = list(scenario.issues)
    warnings: list[str] = [
        "空值一律按“未知”处理，不视为 0 或满足条件。",
        "口头预留不计入可用容量；只有维护状态为 ready 的单元参与分配。",
    ]

    for issue in issues:
        if issue.severity == "error":
            raise PlanError(issue.message)

    confirmations = {
        token: resolve_confirmation(household, scenario.contacts_for(token), as_of)
        for token, household in scenario.households.items()
    }

    # 单元按 ID 稳定排序，保证结果可复现。
    units = sorted(scenario.units.values(), key=lambda u: u.unit_id)
    households = sorted(scenario.households.values(), key=lambda h: (h.move_start or date.max, h.household_token))

    occupied: dict[str, list[tuple[date, date, str]]] = defaultdict(list)
    assignments: list[Assignment] = []
    released: list[ReleasedAssignment] = []
    unplaced: list[Household] = []

    # ---- 1. 只保留仍然有效的已确认安排，先占住单元，避免重复分配 ----
    house_by_token = {h.household_token: h for h in households}
    retained_tokens: set[str] = set()
    for claim in sorted(prior, key=lambda p: (p.start, p.unit_id, p.household_token)):
        household = house_by_token.get(claim.household_token)
        unit = scenario.units.get(claim.unit_id)
        reason = None
        if household is None:
            reason = "家庭已不在输入中"
        elif unit is None:
            reason = "单元已不在输入中"
        elif household.household_token in retained_tokens:
            reason = "同一家庭存在重复的历史安排，仅保留第一条"
        elif any(overlaps(claim.start, claim.end, start, end)
                 for start, end, _ in occupied.get(unit.unit_id, ())):
            reason = (
                "该房间在重叠时段已有另一条仍有效的确认安排；"
                "为避免同一房间同时分配给两户，此条转为待处理冲突"
            )
        else:
            state = confirmations[household.household_token]
            if not state.is_confirmed:
                reason = f"确认不再有效（{state.effective.label}）：{state.evidence}"
            elif household.move_start is None or household.move_end is None:
                reason = "家庭搬迁日期未知，无法核实原安排"
            elif claim.start > household.move_start or claim.end < household.move_end:
                reason = "原安排未覆盖修订后的完整搬迁期"
            else:
                problem = _unit_operational_problem(
                    unit, claim.start, claim.end, scenario.shutdowns_for(unit.unit_id)
                )
                if problem:
                    reason = f"单元条件变化：{problem[1]}"
                elif unit.held_for and unit.held_for != household.household_token:
                    reason = f"单元已改为口头预留给 {unit.held_for}"
                else:
                    evaluation = _evaluate_pair(
                        household, unit, state,
                        {k: v for k, v in occupied.items() if k != unit.unit_id},
                        scenario.shutdowns_for(unit.unit_id),
                    )
                    if not evaluation.eligible:
                        reason = "原安排不再满足硬性条件：" + "；".join(
                            BLOCK_LABELS.get(code, code) for code in evaluation.blockers
                        )
                    elif evaluation.status is not AssignmentStatus.CONFIRMED:
                        reason = (
                            "原安排存在未知字段（无障碍需求或照护距离等），"
                            "不能继续视为已确认；需先核实再锁定"
                        )
                    else:
                        occupied[unit.unit_id].append((claim.start, claim.end, household.household_token))
                        retained_tokens.add(household.household_token)
                        explanation = explanation_for(household, unit, evaluation, rank=1, alternatives=())
                        assignments.append(Assignment(
                            household_token=household.household_token,
                            unit_id=unit.unit_id,
                            start=claim.start,
                            end=claim.end,
                            status=AssignmentStatus.CONFIRMED,
                            confirmation=state.effective,
                            score=evaluation.score,
                            explanation=explanation,
                            retained=True,
                            confirmation_evidence=state.evidence,
                            unit_revision=unit.revision,
                            household_revision=household.revision,
                        ))
        if reason is not None:
            released.append(ReleasedAssignment(
                household_token=claim.household_token,
                unit_id=claim.unit_id,
                start=claim.start,
                end=claim.end,
                reason=reason,
            ))

    # ---- 2. 其余家庭按“无障碍需求优先、确认优先、最受约束优先”分阶段分配 ----
    #
    # 阶段 A/B 先安排有无障碍需求的家庭；阶段 C 安排无该需求的家庭。
    # 每个阶段内部“已确认”先于“待协商/未知”。这样待协商家庭不会挤掉
    # 已确认家庭的房源，普通家庭也只在没有非无障碍单元时才动用无障碍单元。
    active = [
        h for h in households
        if h.household_token not in retained_tokens
        and confirmations[h.household_token].effective.is_actionable
    ]
    inactive = [
        h for h in households
        if confirmations[h.household_token].effective in {Confirmation.DECLINED, Confirmation.UNREACHABLE}
    ]
    for household in inactive:
        unplaced.append(household)

    def constrained_key(household: Household) -> tuple:
        state = confirmations[household.household_token]
        feasible = 0
        for unit in units:
            evaluation = _evaluate_pair(
                household, unit, state, occupied, scenario.shutdowns_for(unit.unit_id)
            )
            if evaluation.eligible:
                feasible += 1
        return (
            CONFIRMATION_RANK[state.effective],
            feasible,
            household.move_start or date.max,
            household.household_token,
        )

    def place(household: Household, *, unit_filter=None) -> bool:
        state = confirmations[household.household_token]
        candidates = [u for u in units if unit_filter is None or unit_filter(u)]
        evaluations = [
            _evaluate_pair(household, unit, state, occupied, scenario.shutdowns_for(unit.unit_id))
            for unit in candidates
        ]
        eligible = sorted((e for e in evaluations if e.eligible), key=_candidate_rank_key)
        if not eligible:
            return False
        if household.needs_step_free:
            preferred = eligible
        else:
            # 普通家庭优先非无障碍单元，把无障碍单元留给真正需要的家庭。
            non_accessible = [e for e in eligible if e.unit.step_free is False]
            preferred = non_accessible or eligible
        chosen = preferred[0]
        start, end = household.move_start, household.move_end
        assert start is not None and end is not None
        occupied[chosen.unit.unit_id].append((start, end, household.household_token))
        alternatives = tuple(
            f"{e.unit.unit_id}（得分 {e.score}）" for e in preferred[1:5]
        )
        explanation = explanation_for(household, chosen.unit, chosen, rank=1, alternatives=alternatives)
        assignments.append(Assignment(
            household_token=household.household_token,
            unit_id=chosen.unit.unit_id,
            start=start,
            end=end,
            status=chosen.status or AssignmentStatus.PENDING,
            confirmation=state.effective,
            score=chosen.score,
            explanation=explanation,
            retained=False,
            confirmation_evidence=state.evidence,
            unit_revision=chosen.unit.revision,
            household_revision=household.revision,
        ))
        return True

    def is_step_free_unit(unit: Unit) -> bool:
        return unit.step_free is True

    def is_non_step_free_unit(unit: Unit) -> bool:
        return unit.step_free is False

    step_free_households = sorted(
        (h for h in active if h.needs_step_free), key=constrained_key
    )
    other_households = sorted(
        (h for h in active if not h.needs_step_free), key=constrained_key
    )
    placed_tokens: set[str] = set()

    def stage(households: Iterable[Household], unit_filter=None) -> None:
        for household in households:
            if household.household_token in placed_tokens:
                continue
            if place(household, unit_filter=unit_filter):
                placed_tokens.add(household.household_token)

    # A. 有无障碍需求的家庭（含已确认与待协商）先使用无障碍单元。
    stage(step_free_households, is_step_free_unit)
    # B. 已确认的普通家庭优先使用非无障碍单元；没有时再考虑无障碍单元。
    stage([h for h in other_households if confirmations[h.household_token].is_confirmed],
          is_non_step_free_unit)
    stage([h for h in other_households if confirmations[h.household_token].is_confirmed])
    # C. 仍未安排的无障碍需求家庭，才使用非无障碍单元作为最后手段。
    stage(step_free_households)
    # D. 其余待协商/未知确认的家庭。
    stage([h for h in other_households if not confirmations[h.household_token].is_confirmed])

    # 未安排的活跃家庭记录一次即可。
    seen_unplaced = {h.household_token for h in unplaced}
    for household in active:
        if (household.household_token not in placed_tokens
                and household.household_token not in seen_unplaced):
            unplaced.append(household)

    _assert_no_double_booking(assignments)

    assignments.sort(key=assignment_sort_key)
    explanation_index = {
        (a.household_token, a.unit_id): a.explanation for a in assignments
    }
    unmet = _build_unmet(scenario, unplaced, confirmations, units, occupied)
    capacity = _build_capacity(scenario, units, assignments, confirmations, as_of)
    coverage = _build_coverage(scenario, confirmations, assignments)
    shortfalls = _build_shortfalls(scenario, units, assignments, confirmations, as_of)
    batch_links = _batch_links(scenario)
    batches = _build_batches(scenario, assignments, batch_links, dependency_lead_days)
    service_groups = _build_service_groups(scenario, assignments, batch_links)

    warnings.extend(_data_warnings(scenario, confirmations))
    tasks = _build_tasks(
        scenario, confirmations, assignments, unmet, capacity, batches,
        shortfalls, service_groups, released, as_of,
    )
    recommendations = _build_recommendations(
        scenario, confirmations, assignments, unmet, shortfalls, batches, units,
    )

    return PlanResult(
        assignments=assignments,
        unmet=unmet,
        capacity=capacity,
        batches=batches,
        tasks=tasks,
        coverage=coverage,
        service_groups=service_groups,
        released=released,
        shortfalls=shortfalls,
        recommendations=recommendations,
        issues=issues,
        warnings=warnings,
        generated_for=as_of,
        explanation_index=explanation_index,
        batch_links=batch_links,
        unassigned_tokens=sorted({h.household_token for h in unplaced}),
    )


def _assert_no_double_booking(assignments: Iterable[Assignment]) -> None:
    by_unit: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_unit[assignment.unit_id].append(assignment)
    for unit_id, items in by_unit.items():
        ordered = sorted(items, key=lambda a: (a.start, a.end, a.household_token))
        for previous, current in zip(ordered, ordered[1:]):
            if overlaps(previous.start, previous.end, current.start, current.end):
                raise PlanError(
                    f"内部错误：单元 {unit_id} 在重叠时段分配给了 "
                    f"{previous.household_token} 和 {current.household_token}。"
                )
        tokens = [a.household_token for a in ordered]
        if len(tokens) != len(set(tokens)):
            raise PlanError(f"内部错误：单元 {unit_id} 上出现重复家庭安排。")


def explanation_for(household: Household, unit: Unit, evaluation: _Evaluation, *,
                    rank: int, alternatives: Iterable[str]) -> Explanation:
    summary_parts = [
        f"{household.household_token} → {unit.unit_id}",
        f"得分 {evaluation.score}" if evaluation.score is not None else "不可分配",
        f"排序第 {rank}",
    ]
    if evaluation.positives:
        summary_parts.append("；".join(evaluation.positives))
    if evaluation.negatives:
        summary_parts.append("注意：" + "；".join(evaluation.negatives))
    if evaluation.blockers:
        summary_parts.append("阻塞：" + "；".join(
            BLOCK_LABELS.get(code, code) for code in evaluation.blockers
        ))
    return Explanation(
        household_token=household.household_token,
        unit_id=unit.unit_id,
        score=evaluation.score,
        rank=rank,
        assignable=evaluation.eligible,
        summary="｜".join(summary_parts),
        positive=tuple(evaluation.positives),
        negative=tuple(evaluation.negatives) + tuple(
            BLOCK_LABELS.get(code, code) for code in evaluation.blockers
        ),
        alternatives=tuple(alternatives),
    )


# ---------------------------------------------------------------------------
# 未满足需求
# ---------------------------------------------------------------------------


def _build_unmet(scenario: Scenario, unplaced: Iterable[Household],
                 confirmations: Mapping[str, ConfirmationState],
                 units: list[Unit],
                 occupied: Mapping[str, list[tuple[date, date, str]]]) -> list[UnmetNeed]:
    result: list[UnmetNeed] = []
    for household in sorted(set(unplaced), key=lambda h: (h.move_start or date.max, h.household_token)):
        state = confirmations[household.household_token]
        if state.effective in {Confirmation.DECLINED, Confirmation.UNREACHABLE}:
            # 即使暂不分配，也评估“若恢复联系会遇到什么”。这样无障碍缺口
            # 不会被“联系不上”掩盖。评估时不占用任何单元。
            evaluations = [
                _evaluate_pair(household, unit, state, {}, scenario.shutdowns_for(unit.unit_id))
                for unit in units
            ]
            blockers = sorted({
                BLOCK_LABELS.get(code, code)
                for evaluation in evaluations for code in evaluation.blockers
                if code != "not_actionable"
            })
            potential = tuple(blockers)
            if state.effective is Confirmation.DECLINED:
                result.append(UnmetNeed(
                    household_token=household.household_token,
                    confirmation=state.effective,
                    category=UnmetCategory.DECLINED,
                    summary="家庭明确拒绝，不分配单元；释放相关口头预留后可重新评估。",
                    needs_step_free=household.needs_step_free,
                    care_distance_limit=household.care_distance_limit,
                    move_start=household.move_start,
                    move_end=household.move_end,
                    details=(state.evidence,),
                    potential_barriers=potential,
                ))
            else:
                result.append(UnmetNeed(
                    household_token=household.household_token,
                    confirmation=state.effective,
                    category=UnmetCategory.UNREACHABLE,
                    summary="联系不上，暂不占用房源；安排继续联络。",
                    needs_step_free=household.needs_step_free,
                    care_distance_limit=household.care_distance_limit,
                    move_start=household.move_start,
                    move_end=household.move_end,
                    details=(state.evidence,),
                    potential_barriers=potential,
                ))
            continue

        evaluations = [
            _evaluate_pair(household, unit, state, occupied, scenario.shutdowns_for(unit.unit_id))
            for unit in units
        ]
        blocker_counts: dict[str, int] = defaultdict(int)
        for evaluation in evaluations:
            for code in evaluation.blockers:
                blocker_counts[code] += 1
        category = _primary_category(household, evaluations, units, blocker_counts)
        details = tuple(
            f"{BLOCK_LABELS.get(code, code)}：{count} 个单元"
            for code, count in sorted(blocker_counts.items())
        )
        if not details:
            details = ("所有单元均不满足硬性条件。",)
        if household.step_free is None:
            details += ("家庭无障碍需求未知，需先核实。",)
        if household.care_distance_limit is None:
            details += ("家庭照护距离上限未知，需先核实。",)
        summary = "待协商：目前没有可提供的兼容单元。"
        if state.effective is Confirmation.NEGOTIATING:
            summary = "方案待协商：当前没有同时满足硬性条件的可用单元，需提出替代方案。"
        result.append(UnmetNeed(
            household_token=household.household_token,
            confirmation=state.effective,
            category=category,
            summary=summary,
            needs_step_free=household.needs_step_free,
            care_distance_limit=household.care_distance_limit,
            move_start=household.move_start,
            move_end=household.move_end,
            details=details,
            secondary_barriers=tuple(
                f"{BLOCK_LABELS.get(code, code)}（{count}）" for code, count in sorted(blocker_counts.items())
            ),
        ))
    return result


def _primary_category(household: Household, evaluations: list[_Evaluation],
                      units: list[Unit], counts: Mapping[str, int]) -> UnmetCategory:
    """按家庭自身硬性需求筛选候选，再判定真实主因。

    不能因为普通单元“非无台阶”就把所有失败都归为无障碍容量不足：
    若有台阶单元存在，但真正障碍是无障碍单元维修/被占/日期不符，
    应如实报告那一个原因。同理，照护距离先于其他可用性原因。
    """

    if household.move_start is None or household.move_end is None:
        return UnmetCategory.DATA_INCOMPLETE

    by_unit = {evaluation.unit.unit_id: evaluation for evaluation in evaluations}

    def blockers_after(keep) -> list[str]:
        result: list[str] = []
        for unit in units:
            if not keep(unit):
                continue
            result.extend(by_unit[unit.unit_id].blockers)
        return result

    # 1. 无障碍需求：先只看无台阶单元。
    if household.needs_step_free:
        accessible_units = [u for u in units if u.step_free is True]
        unknown_units = [u for u in units if u.step_free is None]
        if not accessible_units:
            if unknown_units and not any(u.step_free is False for u in units):
                return UnmetCategory.DATA_INCOMPLETE
            return UnmetCategory.ACCESSIBLE_UNAVAILABLE
        relevant = blockers_after(lambda u: u.step_free is True)
    else:
        relevant = [code for evaluation in evaluations for code in evaluation.blockers]

    # 2. 照护距离：在剩余候选中只看满足距离上限的单元。
    if household.care_distance_limit is not None:
        within_ids = {
            u.unit_id for u in units
            if (u.service_distance is None or u.service_distance <= household.care_distance_limit)
            and (not household.needs_step_free or u.step_free is True)
        }
        if not within_ids:
            return UnmetCategory.CARE_DISTANCE
        relevant = blockers_after(lambda u: u.unit_id in within_ids)

    # 3. 具体可用性原因，按“容量/预留/维修/日期/信息”的顺序呈现最可行动的一种。
    priority: list[tuple[str, UnmetCategory]] = [
        ("occupied", UnmetCategory.CAPACITY_EXHAUSTED),
        ("held", UnmetCategory.HELD),
        ("maintenance", UnmetCategory.MAINTENANCE),
        ("shutdown", UnmetCategory.SHUTDOWN),
        ("date_window", UnmetCategory.DATE_WINDOW),
        (
            "unknown",
            UnmetCategory.DATA_INCOMPLETE,
        ),
    ]
    unknown_codes = {"unit_status_unknown", "unit_dates_unknown", "household_dates_unknown",
                     "step_unit_unknown", "distance_unit_unknown"}
    for code, category in priority:
        if code == "unknown":
            if any(item in unknown_codes for item in relevant):
                return category
        elif code in relevant:
            return category
    if household.needs_step_free and "step_fail" in relevant:
        return UnmetCategory.ACCESSIBLE_UNAVAILABLE
    if household.care_distance_limit is not None and "distance_fail" in relevant:
        return UnmetCategory.CARE_DISTANCE
    if counts.get("occupied"):
        return UnmetCategory.CAPACITY_EXHAUSTED
    return UnmetCategory.NO_COMPATIBLE_UNIT


# ---------------------------------------------------------------------------
# 容量
# ---------------------------------------------------------------------------


def _assignment_status_on(day: date, assignments: list[Assignment]) -> tuple[str | None, str | None]:
    for assignment in assignments:
        if assignment.start <= day <= assignment.end:
            return assignment.unit_id, assignment.status.value
    return None, None


def _build_capacity(scenario: Scenario, units: list[Unit], assignments: list[Assignment],
                    confirmations: Mapping[str, ConfirmationState], as_of: date) -> list[PeriodCapacity]:
    horizon_start, horizon_end = _planning_horizon(scenario, as_of)
    by_unit: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_unit[assignment.unit_id].append(assignment)

    periods: list[PeriodCapacity] = []
    for period_start, period_end in month_periods(horizon_start, horizon_end):
        stats = defaultdict(int)
        free_runs: dict[str, int] = defaultdict(int)
        current_run: dict[str, int] = defaultdict(int)
        free_units: set[str] = set()
        for unit in units:
            shutdowns = scenario.shutdowns_for(unit.unit_id)
            unit_assignments = by_unit.get(unit.unit_id, [])
            for day in iter_days(period_start, period_end):
                state = _unit_day_state(unit, day, shutdowns)
                if state == "unavailable":
                    current_run[unit.unit_id] = 0
                    continue
                if state == "maintenance":
                    stats["maintenance"] += 1
                    current_run[unit.unit_id] = 0
                    continue
                if state == "unknown":
                    stats["unknown"] += 1
                    current_run[unit.unit_id] = 0
                    continue
                if state == "shutdown":
                    stats["shutdown"] += 1
                    current_run[unit.unit_id] = 0
                    continue

                # 可用期之内、未停运：再看是否被安排或口头预留。
                stats["operational"] += 1
                if unit.step_free is True:
                    stats["accessible_operational"] += 1
                _, status = _assignment_status_on(day, unit_assignments)
                if status == AssignmentStatus.CONFIRMED.value:
                    stats["assigned"] += 1
                    current_run[unit.unit_id] = 0
                elif status == AssignmentStatus.PENDING.value:
                    stats["pending"] += 1
                    current_run[unit.unit_id] = 0
                elif state == "held":
                    stats["held"] += 1
                    current_run[unit.unit_id] = 0
                else:
                    stats["free"] += 1
                    free_units.add(unit.unit_id)
                    current_run[unit.unit_id] += 1
                    free_runs[unit.unit_id] = max(free_runs[unit.unit_id], current_run[unit.unit_id])
                    if unit.step_free is True:
                        stats["accessible_free"] += 1

        requesting = [
            h for h in scenario.households.values()
            if h.move_start and h.move_end and overlaps(h.move_start, h.move_end, period_start, period_end)
            and confirmations[h.household_token].effective.is_actionable
        ]
        unreachable = [
            h for h in scenario.households.values()
            if h.move_start and h.move_end and overlaps(h.move_start, h.move_end, period_start, period_end)
            and confirmations[h.household_token].effective is Confirmation.UNREACHABLE
        ]
        unplaced = [
            h for h in requesting
            if not any(
                a.household_token == h.household_token
                and overlaps(a.start, a.end, period_start, period_end)
                for a in assignments
            )
        ]
        periods.append(PeriodCapacity(
            label=f"{period_start.year}-{period_start.month:02d}",
            start=period_start,
            end=period_end,
            operational_unit_days=stats["operational"],
            free_unit_days=stats["free"],
            assigned_unit_days=stats["assigned"],
            pending_unit_days=stats["pending"],
            held_unit_days=stats["held"],
            maintenance_unit_days=stats["maintenance"],
            unknown_unit_days=stats["unknown"],
            shutdown_unit_days=stats["shutdown"],
            accessible_operational_days=stats["accessible_operational"],
            accessible_free_days=stats["accessible_free"],
            free_units=len(free_units),
            longest_free_run_days=max(free_runs.values(), default=0),
            requesting_households=len(requesting),
            requesting_step_free_households=sum(1 for h in requesting if h.needs_step_free),
            unreachable_households=len(unreachable),
            unreachable_step_free_households=sum(1 for h in unreachable if h.needs_step_free),
            unplaced_active_households=len(unplaced),
            unplaced_step_free_households=sum(1 for h in unplaced if h.needs_step_free),
        ))
    return periods


# ---------------------------------------------------------------------------
# 按需求类别的覆盖情况
# ---------------------------------------------------------------------------


def _coverage_row(label: str, households: list[Household],
                  confirmations: Mapping[str, ConfirmationState],
                  assignments: list[Assignment]) -> NeedCoverage:
    placed_confirmed = placed_pending = 0
    unserved_confirmed = unserved_pending = unserved_unreachable = unserved_declined = 0
    for household in households:
        state = confirmations[household.household_token]
        placed = next(
            (a for a in assignments if a.household_token == household.household_token), None
        )
        if placed is not None:
            if placed.status is AssignmentStatus.CONFIRMED:
                placed_confirmed += 1
            else:
                placed_pending += 1
            continue
        if state.effective is Confirmation.CONFIRMED:
            unserved_confirmed += 1
        elif state.effective is Confirmation.NEGOTIATING:
            unserved_pending += 1
        elif state.effective is Confirmation.UNREACHABLE:
            unserved_unreachable += 1
        elif state.effective is Confirmation.DECLINED:
            unserved_declined += 1
        else:
            unserved_pending += 1
    return NeedCoverage(
        need=label,
        demand_total=len(households),
        demand_confirmed=sum(
            1 for h in households if confirmations[h.household_token].is_confirmed
        ),
        placed_confirmed=placed_confirmed,
        placed_pending=placed_pending,
        unserved_confirmed=unserved_confirmed,
        unserved_pending=unserved_pending,
        unserved_unreachable=unserved_unreachable,
        unserved_declined=unserved_declined,
    )


def _build_coverage(scenario: Scenario, confirmations: Mapping[str, ConfirmationState],
                    assignments: list[Assignment]) -> list[NeedCoverage]:
    all_households = list(scenario.households.values())
    rows = [_coverage_row("全部登记家庭", all_households, confirmations, assignments)]
    rows.append(_coverage_row(
        "无障碍需求家庭", [h for h in all_households if h.needs_step_free], confirmations, assignments,
    ))
    for limit in (10.0, 20.0, 30.0):
        rows.append(_coverage_row(
            f"照护距离上限 ≤ {limit:g}（累计）",
            [h for h in all_households
             if h.care_distance_limit is not None and h.care_distance_limit <= limit],
            confirmations, assignments,
        ))
    rows.append(_coverage_row(
        "照护距离上限未知", [h for h in all_households if h.care_distance_limit is None],
        confirmations, assignments,
    ))
    rows.append(_coverage_row(
        "无障碍需求未知", [h for h in all_households if h.step_free is None],
        confirmations, assignments,
    ))
    return rows


# ---------------------------------------------------------------------------
# 缺口（逐日）
# ---------------------------------------------------------------------------


def _build_shortfalls(scenario: Scenario, units: list[Unit], assignments: list[Assignment],
                      confirmations: Mapping[str, ConfirmationState], as_of: date) -> list[Shortfall]:
    horizon_start, horizon_end = _planning_horizon(scenario, as_of)
    by_unit: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_unit[assignment.unit_id].append(assignment)

    general: list[tuple[date, int]] = []
    accessible: list[tuple[date, int]] = []
    for day in iter_days(horizon_start, horizon_end):
        unplaced = [
            h for h in scenario.households.values()
            if h.move_start and h.move_end and h.move_start <= day <= h.move_end
            and confirmations[h.household_token].effective.is_actionable
            and not any(
                a.household_token == h.household_token and a.start <= day <= a.end
                for a in assignments
            )
        ]
        free_units = 0
        free_accessible = 0
        for unit in units:
            if _unit_day_state(unit, day, scenario.shutdowns_for(unit.unit_id)) != "operational":
                continue
            if any(a.start <= day <= a.end for a in by_unit.get(unit.unit_id, ())):
                continue
            free_units += 1
            if unit.step_free is True:
                free_accessible += 1
        general.append((day, max(0, len(unplaced) - free_units)))
        accessible.append((
            day,
            max(0, sum(1 for h in unplaced if h.needs_step_free) - free_accessible),
        ))

    return [
        _summarise_shortfall("general", general, "普通周转单元"),
        _summarise_shortfall("accessible", accessible, "无障碍周转单元"),
    ]


def _summarise_shortfall(kind: str, series: list[tuple[date, int]], label: str) -> Shortfall:
    positive = [(day, value) for day, value in series if value > 0]
    if not positive:
        return Shortfall(kind=kind, peak=0, first_date=None, last_date=None,
                         note=f"{label}在数据范围内没有超出可用容量。")
    peak = max(value for _, value in positive)
    return Shortfall(
        kind=kind,
        peak=peak,
        first_date=positive[0][0],
        last_date=positive[-1][0],
        note=(
            f"{label}需求超过可用容量，峰值 {peak}；"
            f"影响 {positive[0][0].isoformat()}–{positive[-1][0].isoformat()}。"
            "需要调整施工批次、增加房源或增加服务，不能超额分配。"
        ),
    )


# ---------------------------------------------------------------------------
# 施工批次
# ---------------------------------------------------------------------------


def _build_batches(scenario: Scenario, assignments: list[Assignment],
                   links: Mapping[str, str], dependency_lead_days: int) -> list[BatchState]:
    results: list[BatchState] = []
    for batch_id in sorted(scenario.batches):
        batch = scenario.batches[batch_id]
        reasons: list[str] = []
        conditions: list[str] = []
        dependency_ok = True
        if batch.dependency:
            dependency = scenario.batches.get(batch.dependency)
            if dependency is None:
                dependency_ok = False
                reasons.append(f"依赖批次 {batch.dependency} 不存在")
            elif dependency.planned_start is None:
                dependency_ok = False
                reasons.append(f"依赖批次 {batch.dependency} 缺少计划开始日期")
            else:
                earliest = dependency.planned_start + timedelta(days=dependency_lead_days)
                if batch.planned_start and batch.planned_start < earliest:
                    dependency_ok = False
                    reasons.append(
                        f"依赖批次 {batch.dependency} 最早于 {earliest.isoformat()} 才具备条件，"
                        f"当前计划 {batch.planned_start.isoformat()} 过早"
                    )
                else:
                    conditions.append(
                        f"需等待 {batch.dependency} 于 {dependency.planned_start.isoformat()} 开工后 "
                        f"{dependency_lead_days} 天"
                    )

        linked = [token for token, linked_batch in links.items() if linked_batch == batch_id]
        confirmed_relocated = 0
        pending_relocated = 0
        if batch.planned_start:
            for assignment in assignments:
                if assignment.household_token not in linked:
                    continue
                if assignment.start <= batch.planned_start <= assignment.end:
                    if assignment.status is AssignmentStatus.CONFIRMED:
                        confirmed_relocated += 1
                    else:
                        pending_relocated += 1
        gap = None
        if batch.households_required is None:
            reasons.append("未提供所需搬迁户数，无法核实可开始条件")
        else:
            gap = max(0, batch.households_required - confirmed_relocated)
            if gap > 0:
                reasons.append(
                    f"尚缺 {gap} 户已确认的搬迁安排"
                    + (f"；另有 {pending_relocated} 户仅有待确认方案" if pending_relocated else "")
                )
            if confirmed_relocated + pending_relocated >= batch.households_required:
                conditions.append("待上述家庭确认后即可开始")
        if not linked:
            reasons.append("没有家庭与该批次关联，无法核实搬迁就绪情况")
        if batch.planned_start is None:
            reasons.append("缺少计划开始日期")

        can_start = (
            batch.planned_start is not None
            and batch.households_required is not None
            and gap == 0
            and dependency_ok
            and bool(linked)
        )
        results.append(BatchState(
            batch_id=batch_id,
            block=batch.block,
            planned_start=batch.planned_start,
            households_required=batch.households_required,
            dependency=batch.dependency,
            can_start=can_start,
            dependency_ok=dependency_ok,
            link_mode="explicit" if all(
                scenario.households[t].batch_id == batch_id for t in linked
            ) and linked else ("inferred" if linked else "unlinked"),
            confirmed_relocated=confirmed_relocated,
            pending_relocated=pending_relocated,
            gap=gap,
            reasons=tuple(reasons),
            conditions=tuple(conditions),
        ))
    return results


# ---------------------------------------------------------------------------
# 共享接驳
# ---------------------------------------------------------------------------


def _build_service_groups(scenario: Scenario, assignments: list[Assignment],
                          links: Mapping[str, str]) -> list[ServiceGroup]:
    groups: list[ServiceGroup] = []
    by_batch: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        batch_id = links.get(assignment.household_token)
        if batch_id:
            by_batch[batch_id].append(assignment)
    for batch_id in sorted(by_batch):
        items = sorted(by_batch[batch_id], key=lambda a: (a.start, a.household_token))
        cluster: list[Assignment] = []
        for assignment in items:
            if not cluster:
                cluster = [assignment]
                continue
            cluster_start = max(a.start for a in cluster)
            cluster_end = min(a.end for a in cluster)
            if assignment.start <= cluster_end:
                cluster.append(assignment)
            else:
                groups.extend(_emit_group(batch_id, cluster, len(groups)))
                cluster = [assignment]
        groups.extend(_emit_group(batch_id, cluster, len(groups)))
    return groups


def _emit_group(batch_id: str, cluster: list[Assignment], offset: int) -> list[ServiceGroup]:
    if len(cluster) < 2:
        return []
    start = max(a.start for a in cluster)
    end = min(a.end for a in cluster)
    units = sorted({a.unit_id for a in cluster})
    households = tuple(sorted(a.household_token for a in cluster))
    return [ServiceGroup(
        group_id=f"S-{len(households):02d}-{batch_id}-{offset + 1}",
        label=f"{batch_id} 接驳组（{len(households)} 户）",
        household_tokens=households,
        unit_ids=tuple(units),
        window_start=start,
        window_end=end,
        note=(
            f"可共享接驳服务，但居住在 {len(units)} 个独立单元，不共用居住单元；"
            f"接驳可用窗口 {start.isoformat()}–{end.isoformat()}。"
        ),
    )]


# ---------------------------------------------------------------------------
# 后续联络任务
# ---------------------------------------------------------------------------


def _build_tasks(scenario: Scenario, confirmations: Mapping[str, ConfirmationState],
                 assignments: list[Assignment], unmet: list[UnmetNeed],
                 capacity: list[PeriodCapacity], batches: list[BatchState],
                 shortfalls: list[Shortfall], service_groups: list[ServiceGroup],
                 released: list[ReleasedAssignment], as_of: date) -> list[Task]:
    raw: list[dict] = []

    def add(kind: str, priority: TaskPriority, owner: str, subject: str, action: str,
            due: date | None, *, households: Iterable[str] = (), units: Iterable[str] = (),
            batches_: Iterable[str] = (), reason: str = "") -> None:
        raw.append({
            "kind": kind, "priority": priority, "owner": owner, "subject": subject,
            "action": action, "due": due, "households": tuple(households),
            "units": tuple(units), "batches": tuple(batches_), "reason": reason,
        })

    assignment_by_token = {a.household_token: a for a in assignments}
    for household in scenario.households.values():
        token = household.household_token
        state = confirmations[token]
        assignment = assignment_by_token.get(token)
        due = (household.move_start - timedelta(days=14)) if household.move_start else None
        if state.effective is Confirmation.UNREACHABLE:
            add("outreach", TaskPriority.HIGH, "社区联络员",
                f"继续联系 {token}", "通过备用电话/上门核实搬迁意愿与需求。",
                due, households=(token,), reason=state.evidence)
        elif state.effective is Confirmation.DECLINED:
            add("outreach", TaskPriority.LOW, "社区联络员",
                f"记录 {token} 的拒绝", "确认拒绝原因，释放相关口头预留；如政策要求可保留申诉渠道。",
                None, households=(token,), reason=state.evidence)
        elif state.effective is Confirmation.NEGOTIATING:
            add("negotiation", TaskPriority.HIGH, "社区联络员",
                f"与 {token} 协商方案", "说明可用选项，记录新的搬迁日期或无障碍需求。",
                due, households=(token,), reason=state.evidence)
        elif state.effective is Confirmation.UNKNOWN:
            add("reconfirm", TaskPriority.HIGH, "社区联络员",
                f"重新确认 {token}", "原确认失效或缺失，必须重新取得书面确认后才能保留单元。",
                due, households=(token,), reason=state.evidence)
        if assignment and assignment.status is AssignmentStatus.PENDING:
            add("reconfirm", TaskPriority.HIGH, "社区联络员",
                f"确认待定安排 {token} → {assignment.unit_id}",
                "在待确认方案基础上取得居民确认；确认前不视为已安置。",
                due, households=(token,), units=(assignment.unit_id,),
                reason=assignment.confirmation_evidence)
        if (household.step_free is None or household.care_distance_limit is None) and assignment:
            missing = []
            if household.step_free is None:
                missing.append("无障碍需求")
            if household.care_distance_limit is None:
                missing.append("照护距离上限")
            add("verify_need", TaskPriority.MEDIUM, "社区评估员",
                f"核实 {token} 的{'、'.join(missing)}",
                "未知需求不能按“不需要”处理；核实后方可转为已确认安排。",
                due, households=(token,), units=(assignment.unit_id,))

    for unmet_need in unmet:
        if unmet_need.confirmation is Confirmation.NEGOTIATING:
            add("negotiation", TaskPriority.HIGH, "社区联络员",
                f"为 {unmet_need.household_token} 寻找替代方案",
                "当前无兼容单元；讨论调整日期、临时照护安排或新增房源。",
                (unmet_need.move_start - timedelta(days=14)) if unmet_need.move_start else None,
                households=(unmet_need.household_token,), reason=unmet_need.summary)
        elif unmet_need.confirmation is Confirmation.UNREACHABLE and unmet_need.needs_step_free:
            add("capacity", TaskPriority.MEDIUM, "社区评估员",
                f"为 {unmet_need.household_token} 预判无障碍方案",
                "该家庭联系不上但有无障碍需求；恢复联系前先核实是否有可用无障碍单元，"
                "避免恢复后才发现无房可派。",
                (unmet_need.move_start - timedelta(days=21)) if unmet_need.move_start else None,
                households=(unmet_need.household_token,), reason=unmet_need.summary)

    for release in released:
        add("release", TaskPriority.HIGH, "住房协调员",
            f"处理已释放安排 {release.household_token} → {release.unit_id}",
            "原安排已失效，需重新确认并通知居民；在此期间该单元可参与重算，"
            "但同一房间不得同时保留给两户。",
            None, households=(release.household_token,), units=(release.unit_id,),
            reason=release.reason)

    for unit in scenario.units.values():
        if unit.held_for:
            add("verify_hold", TaskPriority.MEDIUM, "住房协调员",
                f"核实 {unit.unit_id} 的口头预留",
                f"预留对象 {unit.held_for}；口头预留不占容量也不等于确认，需转为书面确认或释放。",
                None, units=(unit.unit_id,), households=(unit.held_for,),
                reason="口头预留可能过期或被重复承诺。")
        if unit.maintenance_status is MaintenanceStatus.MAINTENANCE:
            add("maintenance", TaskPriority.MEDIUM, "住房协调员",
                f"确认 {unit.unit_id} 维修完成日期",
                "维修完成前该单元不计入可用容量；如延期需调整批次或补充房源。",
                None, units=(unit.unit_id,))
        elif unit.maintenance_status is MaintenanceStatus.UNKNOWN:
            add("maintenance", TaskPriority.MEDIUM, "住房协调员",
                f"确认 {unit.unit_id} 维护状态",
                "状态未知不能视为可入住；核实后再纳入容量。",
                None, units=(unit.unit_id,))

    for batch in batches:
        if not batch.can_start:
            add("batch", TaskPriority.HIGH, "施工协调员",
                f"复核 {batch.batch_id} 可开始条件",
                "；".join(batch.reasons) or "条件未满足。",
                batch.planned_start, batches_=(batch.batch_id,))
    for shortfall in shortfalls:
        if shortfall.peak > 0:
            add("capacity", TaskPriority.HIGH, "项目负责人",
                f"处理{('无障碍' if shortfall.kind == 'accessible' else '周转')}容量缺口",
                shortfall.note, shortfall.first_date)
    for group in service_groups:
        add("transport", TaskPriority.LOW, "社区联络员",
            f"确认 {group.group_id} 共享接驳",
            group.note, group.window_start, households=group.household_tokens,
            units=group.unit_ids)

    priority_order = {TaskPriority.HIGH: 0, TaskPriority.MEDIUM: 1, TaskPriority.LOW: 2}
    raw.sort(key=lambda item: (
        priority_order[item["priority"]], item["kind"], item["subject"],
        item["due"] or date.max, item["households"], item["units"],
    ))
    return [
        Task(
            task_id=f"T-{index:03d}",
            kind=item["kind"],
            priority=item["priority"],
            owner_role=item["owner"],
            subject=item["subject"],
            action=item["action"],
            due=item["due"],
            related_households=item["households"],
            related_units=item["units"],
            related_batches=item["batches"],
            reason=item["reason"],
        )
        for index, item in enumerate(raw, start=1)
    ]


# ---------------------------------------------------------------------------
# 建议
# ---------------------------------------------------------------------------


def _build_recommendations(scenario: Scenario, confirmations: Mapping[str, ConfirmationState],
                           assignments: list[Assignment], unmet: list[UnmetNeed],
                           shortfalls: list[Shortfall], batches: list[BatchState],
                           units: list[Unit]) -> list[str]:
    recommendations: list[str] = []
    for shortfall in shortfalls:
        if shortfall.peak == 0:
            continue
        if shortfall.kind == "accessible":
            recommendations.append(
                f"无障碍容量不足（峰值 {shortfall.peak}，{shortfall.first_date}–{shortfall.last_date}）："
                "优先将无障碍施工批次错峰、新增无障碍周转单元或临时无障碍接驳服务；"
                "不得把无障碍需求家庭分配到非无障碍单元来抬高总体安置率。"
            )
        else:
            recommendations.append(
                f"周转容量不足（峰值 {shortfall.peak}，{shortfall.first_date}–{shortfall.last_date}）："
                "可推迟受影响施工批次、增加周转房源或与周边项目共享房源。"
            )
    for batch in batches:
        if batch.can_start:
            continue
        if batch.gap and batch.gap > 0:
            recommendations.append(
                f"施工批次 {batch.batch_id} 尚缺 {batch.gap} 户已确认安排；"
                "先完成待确认家庭的确认，或推迟该批次开始日期。"
            )
        elif not batch.dependency_ok:
            recommendations.append(
                f"施工批次 {batch.batch_id} 的前置批次条件未满足；调整依赖批次计划或推迟开始。"
            )
    stale = [token for token, state in confirmations.items()
             if state.effective is Confirmation.UNKNOWN and state.expires_on]
    if stale:
        recommendations.append(
            "以下家庭的确认已失效，重算时不得沿用原房间：" + "、".join(sorted(stale))
            + "；先重新确认再锁定房源。"
        )
    held = [u for u in units if u.held_for]
    if held:
        recommendations.append(
            "口头预留不占已确认容量，也不能阻止冲突检查：" +
            "、".join(f"{u.unit_id}（{u.held_for}）" for u in sorted(held, key=lambda x: x.unit_id))
            + "；请在施工前转为书面确认或释放。"
        )
    maintenance = [u for u in units if u.maintenance_status is MaintenanceStatus.MAINTENANCE]
    if maintenance:
        recommendations.append(
            "维修中的单元不计入容量：" + "、".join(u.unit_id for u in sorted(maintenance, key=lambda x: x.unit_id))
            + "；如提前完工，可重新运行规划增加容量。"
        )
    negotiating = [n.household_token for n in unmet if n.confirmation is Confirmation.NEGOTIATING]
    if negotiating:
        recommendations.append(
            "方案待协商家庭：" + "、".join(sorted(negotiating))
            + "；应提供替代日期或替代房源，而不是标记为已安置。"
        )
    return recommendations


def _data_warnings(scenario: Scenario, confirmations: Mapping[str, ConfirmationState]) -> list[str]:
    notes: list[str] = []
    for token, state in sorted(confirmations.items()):
        household = scenario.households[token]
        if state.household is not state.effective:
            notes.append(
                f"{token} 家庭表状态为“{state.household.label}”，"
                f"按联络与有效期解析为“{state.effective.label}”（{state.evidence}）。"
            )
        if household.step_free is None:
            notes.append(f"{token} 无障碍需求未知，不能按“无需求”确认。")
        if household.care_distance_limit is None:
            notes.append(f"{token} 照护距离上限未知，不能按“无上限”确认。")
    return notes


# ---------------------------------------------------------------------------
# 解释查询
# ---------------------------------------------------------------------------


def explain_household(scenario: Scenario, household_token: str, *,
                      as_of: date | None = None,
                      prior: Iterable[PriorAssignment] = ()) -> list[CandidateEvaluation]:
    """返回该家庭对所有单元的排序评估（不修改场景）。"""

    as_of = as_of or date.today()
    household = scenario.households.get(household_token)
    if household is None:
        raise KeyError(f"未知家庭：{household_token}")
    plan = build_plan(scenario, prior, as_of=as_of)
    state = resolve_confirmation(household, scenario.contacts_for(household_token), as_of)
    occupied: dict[str, list[tuple[date, date, str]]] = defaultdict(list)
    for assignment in plan.assignments:
        if assignment.household_token != household_token:
            occupied[assignment.unit_id].append(
                (assignment.start, assignment.end, assignment.household_token)
            )
    selected_unit = next(
        (a.unit_id for a in plan.assignments if a.household_token == household_token), None
    )
    evaluations = []
    for unit in sorted(scenario.units.values(), key=lambda u: u.unit_id):
        evaluation = _evaluate_pair(
            household, unit, state, occupied, scenario.shutdowns_for(unit.unit_id)
        )
        evaluations.append(evaluation)
    evaluations.sort(key=_candidate_rank_key)
    result: list[CandidateEvaluation] = []
    for rank, evaluation in enumerate(evaluations, start=1):
        result.append(CandidateEvaluation(
            household_token=household_token,
            unit_id=evaluation.unit.unit_id,
            rank=rank,
            eligible=evaluation.eligible,
            score=evaluation.score,
            status=evaluation.status,
            blockers=tuple(BLOCK_LABELS.get(code, code) for code in evaluation.blockers),
            positives=evaluation.positives,
            negatives=evaluation.negatives,
            checks=evaluation.checks,
            blocker_codes=evaluation.blockers,
            selected=evaluation.unit.unit_id == selected_unit,
        ))
    return result
