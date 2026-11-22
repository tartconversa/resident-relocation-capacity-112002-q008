"""规划器的安全与可解释性测试。"""

from __future__ import annotations

import copy
import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 项目未安装为包时，让 `python -m unittest discover -s tests` 也能导入 src 布局。
sys.path.insert(0, str(ROOT / "src"))

from renewal_analytics.domain import (  # noqa: E402
    AssignmentStatus,
    Confirmation,
    ContactEntry,
    Household,
    MaintenanceStatus,
    Outcome,
    PriorAssignment,
    Scenario,
    Unit,
    UnmetCategory,
    WorkBatch,
)
from renewal_analytics.loader import load_scenario  # noqa: E402
from renewal_analytics.planner import (  # noqa: E402
    PlanError,
    build_plan,
    explain_household,
    overlaps,
    resolve_confirmation,
)

AS_OF = date(2026, 11, 20)


def base_scenario() -> Scenario:
    return load_scenario(ROOT / "data")


class FixturePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = base_scenario()
        self.result = build_plan(self.scenario, as_of=AS_OF)

    def test_fixture_assigns_confirmed_and_pending_separately(self):
        confirmed = {a.household_token for a in self.result.confirmed_assignments}
        pending = {a.household_token for a in self.result.pending_assignments}
        self.assertIn("H-1", confirmed)
        self.assertIn("H-2", pending)
        self.assertNotIn("H-3", confirmed | pending)

    def test_each_unit_has_at_most_one_household_at_a_time(self):
        by_unit: dict[str, list] = {}
        for assignment in self.result.assignments:
            by_unit.setdefault(assignment.unit_id, []).append(assignment)
        for unit_id, items in by_unit.items():
            for earlier, later in zip(items, items[1:]):
                self.assertFalse(
                    overlaps(earlier.start, earlier.end, later.start, later.end),
                    f"{unit_id} double booked",
                )

    def test_unreachable_is_not_confirmed(self):
        needs = {need.household_token: need for need in self.result.unmet}
        self.assertIn("H-3", needs)
        self.assertIs(needs["H-3"].confirmation, Confirmation.UNREACHABLE)
        self.assertIs(needs["H-3"].category, UnmetCategory.UNREACHABLE)

    def test_unreachable_household_still_shows_accessibility_barriers(self):
        need = next(n for n in self.result.unmet if n.household_token == "H-3")
        self.assertTrue(need.needs_step_free)
        self.assertTrue(need.potential_barriers)
        joined = "；".join(need.potential_barriers)
        self.assertIn("无台阶", joined)

    def test_capacity_does_not_count_maintenance_or_hold_as_free(self):
        periods = {period.label: period for period in self.result.capacity}
        january = periods["2027-01"]
        # U-2 在 1 月被口头预留 / 待确认，U-3 维修中，U-1 已确认占用。
        self.assertGreater(january.held_unit_days + january.pending_unit_days, 0)
        self.assertEqual(january.maintenance_unit_days, 0)
        self.assertLessEqual(january.free_unit_days, january.operational_unit_days)

    def test_batch_cannot_start_while_confirmation_missing(self):
        batches = {batch.batch_id: batch for batch in self.result.batches}
        self.assertFalse(batches["B-1"].can_start)
        self.assertGreater(batches["B-1"].gap, 0)
        self.assertFalse(batches["B-2"].dependency_ok)

    def test_service_sharing_never_shares_unit(self):
        for group in self.result.service_groups:
            self.assertEqual(len(group.unit_ids), len(group.household_tokens))

    def test_explanations_are_offered_for_each_candidate(self):
        evaluations = explain_household(self.scenario, "H-2", as_of=AS_OF)
        self.assertEqual(len(evaluations), 3)
        self.assertTrue(evaluations[0].eligible)
        self.assertTrue(evaluations[0].selected)
        self.assertFalse(evaluations[1].eligible)
        self.assertTrue(any("已被安排" in blocker for blocker in evaluations[1].blockers))


class ConfirmationRevisionTests(unittest.TestCase):
    def test_accepted_confirmation_expires(self):
        household = Household(
            household_token="H-X", move_start=date(2027, 1, 1), move_end=date(2027, 2, 1),
            step_free=False, care_distance_limit=None, confirmation=Confirmation.CONFIRMED,
        )
        old = ContactEntry(
            contact_id="C-1", household_token="H-X", contacted_at=date(2026, 1, 1),
            outcome=Outcome.ACCEPTED, valid_to=date(2026, 2, 1),
        )
        state = resolve_confirmation(household, [old], as_of=date(2026, 6, 1))
        self.assertIs(state.effective, Confirmation.UNKNOWN)
        self.assertIn("失效", state.evidence)

    def test_household_revision_after_contact_requires_reconfirmation(self):
        household = Household(
            household_token="H-X", move_start=date(2027, 1, 1), move_end=date(2027, 2, 1),
            step_free=False, care_distance_limit=None, confirmation=Confirmation.CONFIRMED,
            revised_at=date(2026, 11, 15),
        )
        accepted = ContactEntry(
            contact_id="C-1", household_token="H-X", contacted_at=date(2026, 11, 1),
            outcome=Outcome.ACCEPTED,
        )
        state = resolve_confirmation(household, [accepted], as_of=AS_OF)
        self.assertIs(state.effective, Confirmation.UNKNOWN)
        self.assertIn("重新确认", state.evidence)

    def test_latest_contact_wins_over_stale_acceptance(self):
        household = Household(
            household_token="H-X", move_start=date(2027, 1, 1), move_end=date(2027, 2, 1),
            step_free=False, care_distance_limit=None, confirmation=Confirmation.CONFIRMED,
        )
        contacts = [
            ContactEntry("C-1", "H-X", date(2026, 10, 1), Outcome.ACCEPTED),
            ContactEntry("C-10", "H-X", date(2026, 11, 1), Outcome.DECLINED),
        ]
        state = resolve_confirmation(household, contacts, as_of=AS_OF)
        self.assertIs(state.effective, Confirmation.DECLINED)


class ReplanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = base_scenario()

    def test_prior_confirmed_assignment_is_retained_without_duplicate(self):
        prior = [PriorAssignment("H-1", "U-1", date(2027, 1, 2), date(2027, 2, 10))]
        result = build_plan(self.scenario, prior, as_of=AS_OF)
        retained = [a for a in result.assignments if a.retained]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].unit_id, "U-1")
        self.assertEqual(sum(1 for a in result.assignments if a.unit_id == "U-1"), 1)

    def test_expired_confirmation_releases_room_instead_of_duplicating(self):
        prior = [PriorAssignment("H-2", "U-2", date(2027, 1, 15), date(2027, 2, 20))]
        # H-2 只有“要求替代方案”，不是已确认，因此旧安排必须释放。
        result = build_plan(self.scenario, prior, as_of=AS_OF)
        self.assertTrue(result.released)
        self.assertEqual(result.released[0].household_token, "H-2")
        self.assertFalse(any(a.retained for a in result.assignments))
        self.assertEqual(sum(1 for a in result.assignments if a.unit_id == "U-2"), 1)
        self.assertTrue(any(task.kind == "release" for task in result.tasks))

    def test_changed_move_dates_release_previous_room(self):
        changed = copy.deepcopy(self.scenario)
        moved = replace(
            changed.households["H-1"],
            move_start=date(2027, 1, 20), move_end=date(2027, 2, 20),
        )
        changed.households["H-1"] = moved
        prior = [PriorAssignment("H-1", "U-1", date(2027, 1, 2), date(2027, 2, 10))]
        result = build_plan(changed, prior, as_of=AS_OF)
        self.assertTrue(result.released)
        self.assertIn("未覆盖", result.released[0].reason)

    def test_overlapping_prior_assignments_do_not_share_room(self):
        prior = [
            PriorAssignment("H-1", "U-1", date(2027, 1, 2), date(2027, 2, 10)),
            PriorAssignment("H-2", "U-1", date(2027, 1, 15), date(2027, 2, 20)),
        ]
        scenario = copy.deepcopy(self.scenario)
        # 让 H-2 也成为“已确认”，制造两条冲突的确认单。
        scenario.contacts.append(ContactEntry(
            "C-9", "H-2", date(2026, 11, 10), Outcome.ACCEPTED,
        ))
        result = build_plan(scenario, prior, as_of=AS_OF)
        u1 = [a for a in result.assignments if a.unit_id == "U-1"]
        self.assertEqual(len(u1), 1)
        self.assertTrue(any("冲突" in release.reason for release in result.released))

    def test_children_batch_and_tasks_change_with_assignment(self):
        prior = [PriorAssignment("H-2", "U-2", date(2027, 1, 15), date(2027, 2, 20))]
        result = build_plan(self.scenario, prior, as_of=AS_OF)
        batch = next(b for b in result.batches if b.batch_id == "B-1")
        self.assertEqual(batch.confirmed_relocated, 1)
        self.assertEqual(batch.pending_relocated, 1)
        self.assertGreater(batch.gap, 0)


class CapacityTests(unittest.TestCase):
    def test_insufficient_capacity_reports_shortfall_not_overallocation(self):
        scenario = Scenario(
            households={
                "H-1": Household("H-1", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
                "H-2": Household("H-2", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-1": Unit("U-1", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.READY),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertEqual(len(result.assignments), 1)
        self.assertEqual(len(result.unmet), 1)
        shortfall = next(s for s in result.shortfalls if s.kind == "general")
        self.assertGreater(shortfall.peak, 0)
        self.assertTrue(any("容量不足" in item or "增加周转房源" in item
                            for item in result.recommendations))

    def test_step_free_shortfall_is_reported_separately(self):
        scenario = Scenario(
            households={
                "H-1": Household("H-1", date(2027, 1, 1), date(2027, 1, 10), True, 10,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-1": Unit("U-1", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.READY),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertFalse(result.assignments)
        shortfall = next(s for s in result.shortfalls if s.kind == "accessible")
        self.assertGreater(shortfall.peak, 0)
        coverage = next(row for row in result.coverage if row.need == "无障碍需求家庭")
        self.assertEqual(coverage.unserved_confirmed, 1)
        self.assertEqual(coverage.placed_confirmed, 0)

    def test_maintenance_and_unknown_are_never_capacity(self):
        scenario = Scenario(
            households={
                "H-1": Household("H-1", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-M": Unit("U-M", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.MAINTENANCE),
                "U-U": Unit("U-U", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.UNKNOWN),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertFalse(result.assignments)
        period = result.capacity[0]
        self.assertEqual(period.free_unit_days, 0)
        self.assertGreater(period.maintenance_unit_days, 0)
        self.assertGreater(period.unknown_unit_days, 0)

    def test_shutdown_window_blocks_assignment(self):
        scenario = Scenario(
            households={
                "H-1": Household("H-1", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-1": Unit("U-1", date(2027, 1, 1), date(2027, 1, 31), False, 5,
                            MaintenanceStatus.READY),
            },
            batches={},
            shutdowns={"U-1": ((date(2027, 1, 5), date(2027, 1, 6)),)},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertFalse(result.assignments)
        period = result.capacity[0]
        self.assertGreater(period.shutdown_unit_days, 0)


class FairnessTests(unittest.TestCase):
    def test_step_free_household_gets_accessible_unit_even_when_ordinary_confirmed(self):
        scenario = Scenario(
            households={
                "H-A": Household("H-A", date(2027, 1, 1), date(2027, 1, 10), True, None,
                                 Confirmation.NEGOTIATING),
                "H-B": Household("H-B", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-A": Unit("U-A", date(2027, 1, 1), date(2027, 1, 10), True, 5,
                            MaintenanceStatus.READY),
                "U-B": Unit("U-B", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.READY),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        by_household = {a.household_token: a.unit_id for a in result.assignments}
        self.assertEqual(by_household["H-A"], "U-A")
        self.assertEqual(by_household["H-B"], "U-B")

    def test_ordinary_household_avoids_accessible_unit_when_possible(self):
        scenario = Scenario(
            households={
                "H-B": Household("H-B", date(2027, 1, 1), date(2027, 1, 10), False, None,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-A": Unit("U-A", date(2027, 1, 1), date(2027, 1, 10), True, 1,
                            MaintenanceStatus.READY),
                "U-B": Unit("U-B", date(2027, 1, 1), date(2027, 1, 10), False, 9,
                            MaintenanceStatus.READY),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertEqual(result.assignments[0].unit_id, "U-B")

    def test_unknown_step_free_need_is_pending_not_confirmed(self):
        scenario = Scenario(
            households={
                "H-1": Household("H-1", date(2027, 1, 1), date(2027, 1, 10), None, 15,
                                 Confirmation.CONFIRMED),
            },
            units={
                "U-1": Unit("U-1", date(2027, 1, 1), date(2027, 1, 10), False, 5,
                            MaintenanceStatus.READY),
            },
            batches={},
        )
        result = build_plan(scenario, as_of=AS_OF)
        self.assertEqual(len(result.assignments), 1)
        self.assertIs(result.assignments[0].status, AssignmentStatus.PENDING)


class LoaderContractTests(unittest.TestCase):
    def test_empty_field_is_unknown_not_zero(self):
        scenario = base_scenario()
        self.assertIsNone(scenario.units["U-3"].held_for)
        self.assertIsNone(scenario.contacts[2].proposal_id)
        self.assertIs(scenario.units["U-3"].maintenance_status, MaintenanceStatus.MAINTENANCE)

    def test_blank_step_free_and_distance_are_unknown(self):
        import tempfile
        from renewal_analytics.loader import load_scenario as load

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "households.csv").write_text(
                "household_token,move_start,move_end,step_free,care_distance_limit,confirmation\n"
                "H-1,2027-01-01,2027-01-10,,,confirmed\n",
                encoding="utf-8",
            )
            (root / "units.csv").write_text(
                "unit_id,available_from,available_to,step_free,service_distance,maintenance_status,held_for\n"
                "U-1,2027-01-01,2027-01-10,,,ready,\n",
                encoding="utf-8",
            )
            (root / "work-batches.csv").write_text(
                "batch_id,block,planned_start,households_required,dependency\n",
                encoding="utf-8",
            )
            (root / "contact-log.csv").write_text(
                "contact_id,household_token,contacted_at,outcome,proposal_id\n",
                encoding="utf-8",
            )
            scenario = load(root)
        household = scenario.households["H-1"]
        unit = scenario.units["U-1"]
        self.assertIsNone(household.step_free)
        self.assertIsNone(household.care_distance_limit)
        self.assertIsNone(unit.step_free)
        self.assertIsNone(unit.service_distance)
        self.assertFalse(scenario.issues and any(i.severity == "error" for i in scenario.issues))

    def test_revision_cycle_is_fatal(self):
        scenario = base_scenario()
        scenario.batches["B-1"] = replace(scenario.batches["B-1"], dependency="B-2")
        scenario.batches["B-2"] = replace(scenario.batches["B-2"], dependency="B-1")
        from renewal_analytics.domain import DataIssue
        scenario.issues.append(DataIssue(
            severity="error", code="dependency_cycle",
            message="施工批次依赖形成环：B-1 → B-2 → B-1。",
        ))
        with self.assertRaises(PlanError):
            build_plan(scenario, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main()
