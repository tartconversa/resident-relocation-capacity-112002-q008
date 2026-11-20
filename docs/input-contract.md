# Input material

All records are synthetic and refer to no real household, project, or city. A file is a source extract, not a precomputed answer. Preserve source identifiers and revisions when interpreting it.

- `households.csv`: CSV; columns `household_token, move_start, move_end, step_free, care_distance_limit, confirmation`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `units.csv`: CSV; columns `unit_id, available_from, available_to, step_free, service_distance, maintenance_status, held_for`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `work-batches.csv`: CSV; columns `batch_id, block, planned_start, households_required, dependency`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `contact-log.csv`: CSV; columns `contact_id, household_token, contacted_at, outcome, proposal_id`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
