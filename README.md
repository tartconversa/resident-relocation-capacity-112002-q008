# Resident Relocation Capacity

面向老旧小区集中施工的周转住房安排工具：分别说明**可用容量**、**已确认/待确认匹配**、**未满足需求**，并让施工批次与社区联络任务随安排变化。仅使用 Python 标准库。

## 运行环境

需要 Python 3.12 或更新版本。

```bash
python -m unittest discover -s tests -v
```

## 生成安排报告

```bash
PYTHONPATH=src python3 -m renewal_analytics.cli --as-of 2026-11-20
```

常用参数：

```bash
# 机器可读结果
PYTHONPATH=src python3 -m renewal_analytics.cli --as-of 2026-11-20 --format json

# 解释某个家庭为何得到该单元、其他单元为何不可行
PYTHONPATH=src python3 -m renewal_analytics.cli --as-of 2026-11-20 --explain H-2

# 基于上一次安排重算（确认/单元状态变化后）
PYTHONPATH=src python3 -m renewal_analytics.cli --as-of 2026-11-20 --prior previous.csv

# 写入文件
PYTHONPATH=src python3 -m renewal_analytics.cli --as-of 2026-11-20 --output plan.md
```

`previous.csv` 字段：`household_token,unit_id,start,end[,status]`。只有仍然有效、仍覆盖修订后搬迁期、且单元条件未变化的**已确认**安排会被保留；其余会进入“重算时释放的旧安排”并生成处理任务。

## 输出内容

报告依次给出：

1. 需要调整的施工安排或新增服务；
2. 逐日容量缺口（普通/无障碍分别核算）；
3. 各时段容量：可运营、空闲、已确认占用、待确认占用、口头预留、维修、状态未知、停运；
4. 已确认安排与待确认方案（分开列出）；
5. 未满足需求及原因类别；
6. 按需求类别的覆盖情况（不合并为单一成功率）；
7. 施工批次可开始条件；
8. 可共享接驳分组（不共用居住单元）；
9. 重算释放的旧安排；
10. 后续联络与协调任务。

## 安全约束

- 空值按未知处理，不视为 0、`false` 或无限。
- 维修中、状态未知、停运、口头预留都不计入空闲容量。
- 同一单元在重叠日期内最多安排一户；重算后再次校验，发现重复会直接报错。
- 容量不足时只显示缺口和调整建议，不生成超额分配。
- 无障碍需求单独统计；确认失效会让原房间回到可分配池，而不是同时留给两户。

字段与判定规则见 [`docs/input-contract.md`](docs/input-contract.md)。

## 代码结构

- `src/renewal_analytics/domain.py`：领域模型。
- `src/renewal_analytics/loader.py`：CSV 读取、修订与数据问题。
- `src/renewal_analytics/planner.py`：确认解析、匹配、容量、缺口、批次与任务。
- `src/renewal_analytics/report.py`：文本/JSON 报告。
- `src/renewal_analytics/cli.py`：命令行入口。
