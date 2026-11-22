"""老旧小区施工周转安置分析。

公开 API：

- ``load_scenario``：读取 CSV 输入。
- ``build_plan``：生成容量、匹配、未满足需求和批次联动结果。
- ``explain_household``：解释某家庭的单元排序。
"""

from .domain import (
    Assignment,
    AssignmentStatus,
    BatchState,
    Check,
    CheckStatus,
    Confirmation,
    ConfirmationState,
    ContactEntry,
    DataIssue,
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
    WorkBatch,
)
from .loader import InputError, load_scenario
from .planner import PlanError, build_plan, explain_household

__all__ = [
    "Assignment",
    "AssignmentStatus",
    "BatchState",
    "Check",
    "CheckStatus",
    "Confirmation",
    "ConfirmationState",
    "ContactEntry",
    "DataIssue",
    "Household",
    "InputError",
    "MaintenanceStatus",
    "NeedCoverage",
    "Outcome",
    "PeriodCapacity",
    "PlanError",
    "PlanResult",
    "PriorAssignment",
    "ReleasedAssignment",
    "Scenario",
    "ServiceGroup",
    "Shortfall",
    "Task",
    "TaskPriority",
    "Unit",
    "UnmetCategory",
    "UnmetNeed",
    "WorkBatch",
    "build_plan",
    "explain_household",
    "load_scenario",
]
