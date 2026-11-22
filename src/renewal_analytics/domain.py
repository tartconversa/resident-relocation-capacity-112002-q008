"""核心领域模型。

所有日期都是 ``datetime.date``（ISO 8601）。空值表示“未知/未提供”，
绝不等于 0、空字符串可忽略或无限可用期。规划器因此会把不完整信息
视为不可确认条件，而不是默认满足。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Confirmation(str, Enum):
    """家庭侧的确认状态。"""

    CONFIRMED = "confirmed"
    NEGOTIATING = "negotiating"
    UNREACHABLE = "unreachable"
    DECLINED = "declined"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            Confirmation.CONFIRMED: "已确认",
            Confirmation.NEGOTIATING: "方案待协商",
            Confirmation.UNREACHABLE: "联系不上",
            Confirmation.DECLINED: "真实拒绝",
            Confirmation.UNKNOWN: "确认状态未知",
        }[self]

    @property
    def is_actionable(self) -> bool:
        """是否仍应尝试为其安排（拒绝和联系不上的不占用房源）。"""

        return self in {Confirmation.CONFIRMED, Confirmation.NEGOTIATING, Confirmation.UNKNOWN}


class MaintenanceStatus(str, Enum):
    """单元维护状态。只有 READY 的单元可以进入分配。"""

    READY = "ready"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            MaintenanceStatus.READY: "可入住",
            MaintenanceStatus.MAINTENANCE: "维修中",
            MaintenanceStatus.UNKNOWN: "维护状态未知",
        }[self]


class Outcome(str, Enum):
    """一次联络的结果。"""

    ACCEPTED = "accepted"
    ALTERNATIVE_REQUESTED = "alternative_requested"
    NO_ANSWER = "no_answer"
    DECLINED = "declined"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            Outcome.ACCEPTED: "已接受",
            Outcome.ALTERNATIVE_REQUESTED: "要求替代方案",
            Outcome.NO_ANSWER: "无人接听",
            Outcome.DECLINED: "拒绝",
            Outcome.UNREACHABLE: "联系不上",
            Outcome.UNKNOWN: "结果未知",
        }[self]


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            CheckStatus.PASS: "通过",
            CheckStatus.FAIL: "不通过",
            CheckStatus.UNKNOWN: "未知",
        }[self]


class AssignmentStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING = "pending"

    @property
    def label(self) -> str:
        return {
            AssignmentStatus.CONFIRMED: "已确认安排",
            AssignmentStatus.PENDING: "待确认方案",
        }[self]


class UnmetCategory(str, Enum):
    """未满足需求的主因。"""

    DECLINED = "declined"
    UNREACHABLE = "unreachable"
    PENDING_NEGOTIATION = "pending_negotiation"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    ACCESSIBLE_UNAVAILABLE = "accessible_unavailable"
    CARE_DISTANCE = "care_distance"
    DATE_WINDOW = "date_window"
    MAINTENANCE = "maintenance"
    HELD = "held"
    SHUTDOWN = "shutdown"
    DATA_INCOMPLETE = "data_incomplete"
    NO_COMPATIBLE_UNIT = "no_compatible_unit"

    @property
    def label(self) -> str:
        return {
            UnmetCategory.DECLINED: "真实拒绝，不再分配",
            UnmetCategory.UNREACHABLE: "联系不上，暂不占用房源",
            UnmetCategory.PENDING_NEGOTIATION: "方案待协商，尚无可用方案",
            UnmetCategory.CAPACITY_EXHAUSTED: "有兼容单元但已被占用，容量不足",
            UnmetCategory.ACCESSIBLE_UNAVAILABLE: "无障碍需求无可用单元",
            UnmetCategory.CARE_DISTANCE: "没有满足照护距离的单元",
            UnmetCategory.DATE_WINDOW: "没有覆盖完整搬迁日期的单元",
            UnmetCategory.MAINTENANCE: "候选单元维修中",
            UnmetCategory.HELD: "候选单元被口头预留",
            UnmetCategory.SHUTDOWN: "候选单元在搬迁期内停运",
            UnmetCategory.DATA_INCOMPLETE: "关键信息缺失，无法安全确认",
            UnmetCategory.NO_COMPATIBLE_UNIT: "没有同时满足硬性条件的单元",
        }[self]


class TaskPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return {TaskPriority.HIGH: "高", TaskPriority.MEDIUM: "中", TaskPriority.LOW: "低"}[self]


@dataclass(frozen=True)
class Household:
    household_token: str
    move_start: date | None
    move_end: date | None
    step_free: bool | None
    care_distance_limit: float | None
    confirmation: Confirmation
    revision: int = 1
    revised_at: date | None = None
    batch_id: str | None = None
    block: str | None = None
    source_row: int | None = None

    @property
    def needs_step_free(self) -> bool:
        return self.step_free is True

    @property
    def needs_care_distance(self) -> bool:
        return self.care_distance_limit is not None


@dataclass(frozen=True)
class Unit:
    unit_id: str
    available_from: date | None
    available_to: date | None
    step_free: bool | None
    service_distance: float | None
    maintenance_status: MaintenanceStatus
    held_for: str | None = None
    revision: int = 1
    block: str | None = None
    source_row: int | None = None


@dataclass(frozen=True)
class WorkBatch:
    batch_id: str
    block: str
    planned_start: date | None
    households_required: int | None
    dependency: str | None = None
    source_row: int | None = None


@dataclass(frozen=True)
class ContactEntry:
    contact_id: str
    household_token: str
    contacted_at: date | None
    outcome: Outcome
    proposal_id: str | None = None
    note: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    source_row: int | None = None


@dataclass(frozen=True)
class DataIssue:
    severity: str
    code: str
    message: str
    source: str | None = None
    row: int | None = None


@dataclass
class Scenario:
    """一次规划所需的全部输入。

    ``shutdowns`` 用于表达跨多天的服务停运：若某天落在任一停运区间内，
    该单元当天视为不可入住。逐日计算，避免把停运误算成可用容量。
    """

    households: dict[str, Household] = field(default_factory=dict)
    units: dict[str, Unit] = field(default_factory=dict)
    batches: dict[str, WorkBatch] = field(default_factory=dict)
    contacts: list[ContactEntry] = field(default_factory=list)
    shutdowns: dict[str, tuple[tuple[date, date], ...]] = field(default_factory=dict)
    issues: list[DataIssue] = field(default_factory=list)

    def contacts_for(self, household_token: str) -> list[ContactEntry]:
        entries = [c for c in self.contacts if c.household_token == household_token]
        return sorted(entries, key=contact_sort_key)

    def shutdowns_for(self, unit_id: str) -> tuple[tuple[date, date], ...]:
        return self.shutdowns.get(unit_id, ())


def _natural_key(value: str) -> tuple:
    """把 ``C-2`` / ``C-10`` 按数字而不是字典序排序。"""

    parts: list[object] = []
    number = ""
    text = ""
    for char in value:
        if char.isdigit():
            if text:
                parts.append(text)
                text = ""
            number += char
        else:
            if number:
                parts.append(int(number))
                number = ""
            text += char
    if number:
        parts.append(int(number))
    if text:
        parts.append(text)
    return tuple(parts)


def contact_sort_key(entry: ContactEntry) -> tuple:
    # 没有日期的记录最早；同一天内按 contact_id 的自然顺序。
    return (entry.contacted_at or date.min, _natural_key(entry.contact_id))


def household_sort_key(household: Household) -> tuple:
    return (household.move_start or date.max, household.household_token)


def assignment_sort_key(assignment: "Assignment") -> tuple:
    return (assignment.start, assignment.unit_id, assignment.household_token)


@dataclass(frozen=True)
class Check:
    kind: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class ConfirmationState:
    """针对某一时间窗口解析后的确认状态。

    ``household`` 是家庭表声明的状态，``effective`` 是结合有效联络后
    实际可用于规划的状态；两者不同时 ``evidence`` 说明依据。
    """

    household: Confirmation
    effective: Confirmation
    evidence: str
    contact_id: str | None = None
    expires_on: date | None = None
    latest_contact: ContactEntry | None = None

    @property
    def usable(self) -> bool:
        return self.effective.is_actionable

    @property
    def is_confirmed(self) -> bool:
        return self.effective is Confirmation.CONFIRMED


@dataclass(frozen=True)
class CandidateEvaluation:
    """对“某家庭 × 某单元”的一次可解释评估。"""

    household_token: str
    unit_id: str
    rank: int
    eligible: bool
    score: int | None
    status: AssignmentStatus | None
    blockers: tuple[str, ...] = ()
    positives: tuple[str, ...] = ()
    negatives: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    blocker_codes: tuple[str, ...] = ()
    selected: bool = False

    @property
    def explanation(self) -> "Explanation":
        return Explanation(
            household_token=self.household_token,
            unit_id=self.unit_id,
            score=self.score,
            rank=self.rank,
            assignable=self.eligible,
            summary=_candidate_summary(self),
            positive=self.positives,
            negative=self.negatives + self.blockers,
        )


def _candidate_summary(evaluation: CandidateEvaluation) -> str:
    if not evaluation.eligible:
        if evaluation.blockers:
            return "不可分配：" + "；".join(evaluation.blockers)
        return "不可分配：未满足硬性条件。"
    score = evaluation.score if evaluation.score is not None else 0
    delta = "，".join(evaluation.positives) if evaluation.positives else "无额外加权"
    return f"可分配，优先级得分 {score}（{delta}）。"


@dataclass(frozen=True)
class Explanation:
    """一次匹配排序的可读解释。"""

    household_token: str
    unit_id: str
    score: int | None
    rank: int
    assignable: bool
    summary: str
    positive: tuple[str, ...] = ()
    negative: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assignment:
    household_token: str
    unit_id: str
    start: date
    end: date
    status: AssignmentStatus
    confirmation: Confirmation
    score: int | None
    explanation: Explanation
    retained: bool = False
    confirmation_evidence: str = ""
    unit_revision: int = 1
    household_revision: int = 1
    service_shared_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnmetNeed:
    household_token: str
    confirmation: Confirmation
    category: UnmetCategory
    summary: str
    needs_step_free: bool
    care_distance_limit: float | None
    move_start: date | None
    move_end: date | None
    details: tuple[str, ...] = ()
    secondary_barriers: tuple[str, ...] = ()
    potential_barriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeriodCapacity:
    label: str
    start: date
    end: date
    operational_unit_days: int
    free_unit_days: int
    assigned_unit_days: int
    pending_unit_days: int
    held_unit_days: int
    maintenance_unit_days: int
    unknown_unit_days: int
    shutdown_unit_days: int
    accessible_operational_days: int
    accessible_free_days: int
    free_units: int
    longest_free_run_days: int
    requesting_households: int
    requesting_step_free_households: int
    unreachable_households: int
    unreachable_step_free_households: int
    unplaced_active_households: int
    unplaced_step_free_households: int


@dataclass(frozen=True)
class BatchState:
    batch_id: str
    block: str
    planned_start: date | None
    households_required: int | None
    dependency: str | None
    can_start: bool
    dependency_ok: bool
    link_mode: str
    confirmed_relocated: int
    pending_relocated: int
    gap: int | None
    reasons: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    priority: TaskPriority
    owner_role: str
    subject: str
    action: str
    due: date | None
    related_households: tuple[str, ...] = ()
    related_units: tuple[str, ...] = ()
    related_batches: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class NeedCoverage:
    """按需求类别分别统计，不合并成单一成功率。"""

    need: str
    demand_total: int
    demand_confirmed: int
    placed_confirmed: int
    placed_pending: int
    unserved_confirmed: int
    unserved_pending: int
    unserved_unreachable: int
    unserved_declined: int


@dataclass(frozen=True)
class ServiceGroup:
    group_id: str
    label: str
    household_tokens: tuple[str, ...]
    unit_ids: tuple[str, ...]
    window_start: date
    window_end: date
    note: str


@dataclass(frozen=True)
class ReleasedAssignment:
    household_token: str
    unit_id: str
    start: date | None
    end: date | None
    reason: str


@dataclass(frozen=True)
class PriorAssignment:
    """用于重算的上一次安排。"""

    household_token: str
    unit_id: str
    start: date
    end: date
    status: AssignmentStatus = AssignmentStatus.CONFIRMED

    @classmethod
    def from_dict(cls, payload: dict) -> "PriorAssignment":
        return cls(
            household_token=str(payload["household_token"]),
            unit_id=str(payload["unit_id"]),
            start=date.fromisoformat(str(payload["start"])),
            end=date.fromisoformat(str(payload["end"])),
            status=AssignmentStatus(str(payload.get("status", "confirmed"))),
        )


@dataclass(frozen=True)
class Shortfall:
    kind: str
    peak: int
    first_date: date | None
    last_date: date | None
    note: str


@dataclass
class PlanResult:
    assignments: list[Assignment]
    unmet: list[UnmetNeed]
    capacity: list[PeriodCapacity]
    batches: list[BatchState]
    tasks: list[Task]
    coverage: list[NeedCoverage]
    service_groups: list[ServiceGroup]
    released: list[ReleasedAssignment]
    shortfalls: list[Shortfall]
    recommendations: list[str]
    issues: list[DataIssue]
    warnings: list[str]
    generated_for: date | None = None
    explanation_index: dict[tuple[str, str], Explanation] = field(default_factory=dict)
    batch_links: dict[str, str] = field(default_factory=dict)
    unassigned_tokens: list[str] = field(default_factory=list)

    @property
    def confirmed_assignments(self) -> list[Assignment]:
        return [a for a in self.assignments if a.status is AssignmentStatus.CONFIRMED]

    @property
    def pending_assignments(self) -> list[Assignment]:
        return [a for a in self.assignments if a.status is AssignmentStatus.PENDING]
