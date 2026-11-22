"""从合成 CSV 读取规划输入。

约定（与 docs/input-contract.md 一致）：
- 空单元格表示未知/未提供，不解释为 0、False 或无限。
- 同名键的多行视为同一记录的修订；``revision`` 大者生效，
  相同 revision 时文件中靠后的行生效，并产生一条数据问题提示。
- 无法解析的值不会静默丢弃，而是记录为 DataIssue 并把字段置为未知。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from .domain import (
    ContactEntry,
    Confirmation,
    DataIssue,
    Household,
    MaintenanceStatus,
    Outcome,
    Scenario,
    Unit,
    WorkBatch,
)


TRUE_VALUES = {"true", "yes", "y", "1", "是", "有"}
FALSE_VALUES = {"false", "no", "n", "0", "否", "无"}


class InputError(ValueError):
    """无法继续规划的结构性输入错误。"""


def parse_date(value: str | None, *, source: str, row: int, field: str, issues: list[DataIssue]) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        issues.append(DataIssue(
            severity="warning",
            code="bad_date",
            message=f"无法解析日期 {value!r}（字段 {field}），按未知处理。",
            source=source,
            row=row,
        ))
        return None


def parse_bool(value: str | None, *, source: str, row: int, field: str, issues: list[DataIssue]) -> bool | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    issues.append(DataIssue(
        severity="warning",
        code="bad_bool",
        message=f"无法解析布尔值 {value!r}（字段 {field}），按未知处理。",
        source=source,
        row=row,
    ))
    return None


def parse_number(value: str | None, *, source: str, row: int, field: str, issues: list[DataIssue]) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value.strip())
    except ValueError:
        issues.append(DataIssue(
            severity="warning",
            code="bad_number",
            message=f"无法解析数值 {value!r}（字段 {field}），按未知处理。",
            source=source,
            row=row,
        ))
        return None
    if number < 0:
        issues.append(DataIssue(
            severity="warning",
            code="negative_number",
            message=f"字段 {field} 为负数 {value!r}，按未知处理。",
            source=source,
            row=row,
        ))
        return None
    return number


def parse_int(value: str | None, *, source: str, row: int, field: str, issues: list[DataIssue]) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        issues.append(DataIssue(
            severity="warning",
            code="bad_int",
            message=f"无法解析整数 {value!r}（字段 {field}），按未知处理。",
            source=source,
            row=row,
        ))
        return None


def _enum_or_unknown(enum_type, value: str | None, default, *, source: str, row: int,
                     field: str, issues: list[DataIssue]):
    if value is None or not value.strip():
        return default
    try:
        return enum_type(value.strip().lower())
    except ValueError:
        issues.append(DataIssue(
            severity="warning",
            code="bad_enum",
            message=f"未知的 {field} 取值 {value!r}，按未知处理。",
            source=source,
            row=row,
        ))
        return default


def _revision_of(row: Mapping[str, str | None]) -> int:
    raw = (row.get("revision") or "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _read(path: Path) -> list[tuple[int, dict[str, str | None]]]:
    if not path.is_file():
        raise InputError(f"缺少输入文件：{path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise InputError(f"{path.name} 没有表头。")
        rows = []
        for index, raw in enumerate(reader, start=2):
            if raw is None or all(not (value or "").strip() for value in raw.values()):
                continue
            if None in raw or any(value is None for value in raw.values()):
                raise InputError(f"{path.name} 第 {index} 行列数不一致。")
            rows.append((index, raw))
        return rows


def _validate_window(start: date | None, end: date | None, *, source: str, row: int,
                     subject: str, issues: list[DataIssue]) -> None:
    if start is not None and end is not None and end < start:
        issues.append(DataIssue(
            severity="warning",
            code="reversed_window",
            message=f"{subject} 的结束日期早于开始日期，按未知窗口处理。",
            source=source,
            row=row,
        ))


def load_households(path: Path, issues: list[DataIssue]) -> dict[str, Household]:
    households: dict[str, Household] = {}
    for row_number, row in _read(path):
        token = (row.get("household_token") or "").strip()
        if not token:
            raise InputError(f"{path.name} 第 {row_number} 行缺少 household_token。")
        move_start = parse_date(row.get("move_start"), source=path.name, row=row_number,
                                field="move_start", issues=issues)
        move_end = parse_date(row.get("move_end"), source=path.name, row=row_number,
                              field="move_end", issues=issues)
        _validate_window(move_start, move_end, source=path.name, row=row_number,
                         subject=f"家庭 {token}", issues=issues)
        revision = _revision_of(row)
        candidate = Household(
            household_token=token,
            move_start=move_start,
            move_end=move_end,
            step_free=parse_bool(row.get("step_free"), source=path.name, row=row_number,
                                 field="step_free", issues=issues),
            care_distance_limit=parse_number(row.get("care_distance_limit"), source=path.name,
                                             row=row_number, field="care_distance_limit", issues=issues),
            confirmation=_enum_or_unknown(Confirmation, row.get("confirmation"), Confirmation.UNKNOWN,
                                          source=path.name, row=row_number,
                                          field="confirmation", issues=issues),
            revision=revision,
            revised_at=parse_date(row.get("revised_at"), source=path.name, row=row_number,
                                  field="revised_at", issues=issues),
            batch_id=(row.get("batch_id") or "").strip() or None,
            block=(row.get("block") or "").strip() or None,
            source_row=row_number,
        )
        existing = households.get(token)
        if existing is None or candidate.revision >= existing.revision:
            if existing is not None and candidate.revision == existing.revision:
                issues.append(DataIssue(
                    severity="info",
                    code="same_revision",
                    message=f"家庭 {token} 存在相同 revision={candidate.revision} 的重复记录，采用文件中后者。",
                    source=path.name,
                    row=row_number,
                ))
            households[token] = candidate
        else:
            issues.append(DataIssue(
                severity="info",
                code="superseded_row",
                message=f"家庭 {token} 的旧修订（revision={candidate.revision}）已忽略。",
                source=path.name,
                row=row_number,
            ))
    return households


def load_units(path: Path, issues: list[DataIssue]) -> dict[str, Unit]:
    units: dict[str, Unit] = {}
    for row_number, row in _read(path):
        unit_id = (row.get("unit_id") or "").strip()
        if not unit_id:
            raise InputError(f"{path.name} 第 {row_number} 行缺少 unit_id。")
        available_from = parse_date(row.get("available_from"), source=path.name, row=row_number,
                                    field="available_from", issues=issues)
        available_to = parse_date(row.get("available_to"), source=path.name, row=row_number,
                                  field="available_to", issues=issues)
        _validate_window(available_from, available_to, source=path.name, row=row_number,
                         subject=f"单元 {unit_id}", issues=issues)
        candidate = Unit(
            unit_id=unit_id,
            available_from=available_from,
            available_to=available_to,
            step_free=parse_bool(row.get("step_free"), source=path.name, row=row_number,
                                 field="step_free", issues=issues),
            service_distance=parse_number(row.get("service_distance"), source=path.name,
                                          row=row_number, field="service_distance", issues=issues),
            maintenance_status=_enum_or_unknown(
                MaintenanceStatus, row.get("maintenance_status"), MaintenanceStatus.UNKNOWN,
                source=path.name, row=row_number, field="maintenance_status", issues=issues,
            ),
            held_for=(row.get("held_for") or "").strip() or None,
            revision=_revision_of(row),
            block=(row.get("block") or "").strip() or None,
            source_row=row_number,
        )
        existing = units.get(unit_id)
        if existing is None or candidate.revision >= existing.revision:
            if existing is not None and candidate.revision == existing.revision:
                issues.append(DataIssue(
                    severity="info",
                    code="same_revision",
                    message=f"单元 {unit_id} 存在相同 revision={candidate.revision} 的重复记录，采用文件中后者。",
                    source=path.name,
                    row=row_number,
                ))
            units[unit_id] = candidate
        else:
            issues.append(DataIssue(
                severity="info",
                code="superseded_row",
                message=f"单元 {unit_id} 的旧修订（revision={candidate.revision}）已忽略。",
                source=path.name,
                row=row_number,
            ))
    return units


def load_batches(path: Path, issues: list[DataIssue]) -> dict[str, WorkBatch]:
    batches: dict[str, WorkBatch] = {}
    for row_number, row in _read(path):
        batch_id = (row.get("batch_id") or "").strip()
        if not batch_id:
            raise InputError(f"{path.name} 第 {row_number} 行缺少 batch_id。")
        batches[batch_id] = WorkBatch(
            batch_id=batch_id,
            block=(row.get("block") or "").strip(),
            planned_start=parse_date(row.get("planned_start"), source=path.name, row=row_number,
                                     field="planned_start", issues=issues),
            households_required=parse_int(row.get("households_required"), source=path.name,
                                          row=row_number, field="households_required", issues=issues),
            dependency=(row.get("dependency") or "").strip() or None,
            source_row=row_number,
        )
    return batches


def load_contacts(path: Path, issues: list[DataIssue]) -> list[ContactEntry]:
    contacts: list[ContactEntry] = []
    for row_number, row in _read(path):
        contact_id = (row.get("contact_id") or "").strip()
        household_token = (row.get("household_token") or "").strip()
        if not contact_id or not household_token:
            raise InputError(f"{path.name} 第 {row_number} 行缺少 contact_id 或 household_token。")
        valid_from = parse_date(row.get("valid_from"), source=path.name, row=row_number,
                                field="valid_from", issues=issues)
        valid_to = parse_date(row.get("valid_to"), source=path.name, row=row_number,
                              field="valid_to", issues=issues)
        _validate_window(valid_from, valid_to, source=path.name, row=row_number,
                         subject=f"联络 {contact_id}", issues=issues)
        contacts.append(ContactEntry(
            contact_id=contact_id,
            household_token=household_token,
            contacted_at=parse_date(row.get("contacted_at"), source=path.name, row=row_number,
                                    field="contacted_at", issues=issues),
            outcome=_enum_or_unknown(Outcome, row.get("outcome"), Outcome.UNKNOWN,
                                     source=path.name, row=row_number,
                                     field="outcome", issues=issues),
            proposal_id=(row.get("proposal_id") or "").strip() or None,
            note=(row.get("note") or "").strip() or None,
            valid_from=valid_from,
            valid_to=valid_to,
            source_row=row_number,
        ))
    return contacts


def load_shutdowns(path: Path, issues: list[DataIssue]) -> dict[str, tuple[tuple[date, date], ...]]:
    """可选文件 shutdowns.csv：unit_id, shutdown_start, shutdown_end。"""

    if not path.is_file():
        return {}
    grouped: dict[str, list[tuple[date, date]]] = {}
    for row_number, row in _read(path):
        unit_id = (row.get("unit_id") or "").strip()
        start = parse_date(row.get("shutdown_start"), source=path.name, row=row_number,
                           field="shutdown_start", issues=issues)
        end = parse_date(row.get("shutdown_end"), source=path.name, row=row_number,
                         field="shutdown_end", issues=issues)
        if not unit_id:
            raise InputError(f"{path.name} 第 {row_number} 行缺少 unit_id。")
        if start is None or end is None:
            issues.append(DataIssue(
                severity="warning",
                code="incomplete_shutdown",
                message=f"单元 {unit_id} 的停运区间不完整，已忽略。",
                source=path.name,
                row=row_number,
            ))
            continue
        if end < start:
            issues.append(DataIssue(
                severity="warning",
                code="reversed_shutdown",
                message=f"单元 {unit_id} 的停运结束早于开始，已忽略。",
                source=path.name,
                row=row_number,
            ))
            continue
        grouped.setdefault(unit_id, []).append((start, end))
    return {unit: tuple(sorted(windows)) for unit, windows in grouped.items()}


def load_scenario(data_dir: str | Path) -> Scenario:
    root = Path(data_dir)
    issues: list[DataIssue] = []
    households = load_households(root / "households.csv", issues)
    units = load_units(root / "units.csv", issues)
    batches = load_batches(root / "work-batches.csv", issues)
    contacts = load_contacts(root / "contact-log.csv", issues)
    shutdowns = load_shutdowns(root / "shutdowns.csv", issues)

    for contact in contacts:
        if contact.household_token not in households:
            issues.append(DataIssue(
                severity="warning",
                code="orphan_contact",
                message=f"联络 {contact.contact_id} 指向不存在的家庭 {contact.household_token}。",
                source="contact-log.csv",
                row=contact.source_row,
            ))
    for unit in units.values():
        if unit.held_for and unit.held_for not in households:
            issues.append(DataIssue(
                severity="warning",
                code="orphan_hold",
                message=f"单元 {unit.unit_id} 预留给了不存在的家庭 {unit.held_for}。",
                source="units.csv",
                row=unit.source_row,
            ))
    for batch in batches.values():
        if batch.dependency and batch.dependency not in batches:
            issues.append(DataIssue(
                severity="warning",
                code="unknown_dependency",
                message=f"施工批次 {batch.batch_id} 依赖不存在的批次 {batch.dependency}。",
                source="work-batches.csv",
                row=batch.source_row,
            ))
        if batch.households_required is not None and batch.households_required < 0:
            issues.append(DataIssue(
                severity="warning",
                code="negative_required",
                message=f"施工批次 {batch.batch_id} 的 households_required 为负，按未知处理。",
                source="work-batches.csv",
                row=batch.source_row,
            ))

    _check_cycles(batches, issues)
    return Scenario(
        households=households,
        units=units,
        batches=batches,
        contacts=contacts,
        shutdowns=shutdowns,
        issues=issues,
    )


def _check_cycles(batches: Mapping[str, WorkBatch], issues: list[DataIssue]) -> None:
    for batch_id in batches:
        seen: list[str] = []
        current: str | None = batch_id
        while current is not None:
            if current in seen:
                issues.append(DataIssue(
                    severity="error",
                    code="dependency_cycle",
                    message=f"施工批次依赖形成环：{' → '.join(seen + [current])}。",
                    source="work-batches.csv",
                ))
                return
            seen.append(current)
            batch = batches.get(current)
            current = batch.dependency if batch else None


def write_csv(path: Path, fieldnames: Iterable[str], rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in writer.fieldnames})
