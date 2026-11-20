# Resident Relocation Capacity

这是城市更新数据流程的 Python 初始项目，附有合成输入示例和字段说明，尚未包含业务分析结果。

## 运行环境

需要 Python 3.12 或更新版本，仅使用 Python 标准库。

## 检查输入材料

```powershell
python -m unittest discover -s tests -v
```

检查会确认示例 CSV 可读取、字段完整且至少包含一条记录。数据字段说明见 `docs/input-contract.md`。
