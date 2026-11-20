# 合成输入材料

这里的记录均为合成数据，不对应真实住户、项目或城市。每个文件表示一个来源数据表，不包含分析结论；解释记录时应保留来源标识和修订关系。

- `households.csv`：CSV 表格，字段为 `household_token, move_start, move_end, step_free, care_distance_limit, confirmation`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `units.csv`：CSV 表格，字段为 `unit_id, available_from, available_to, step_free, service_distance, maintenance_status, held_for`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `work-batches.csv`：CSV 表格，字段为 `batch_id, block, planned_start, households_required, dependency`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `contact-log.csv`：CSV 表格，字段为 `contact_id, household_token, contacted_at, outcome, proposal_id`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
