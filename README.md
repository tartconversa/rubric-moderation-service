# 阅读作品评分的可比性与复核分派

用 Python 3.11 标准库实现的本地应用，覆盖读取合成资料、逐维裁决、尺度与规则校准、
学校比较、复核分派、资格撤回、发布审计与可追溯报告。所有人员、学校、机构和业务
记录均为演练数据，原始资料 `materials/` 只读，运行状态写入另行指定的位置。

## 运行

```sh
python3 -m reading_review inspect                     # 指纹与三种权威来源
python3 -m reading_review dimensions --work W72       # 逐维状态与依据
python3 -m reading_review triage --dimension evidence --limit 10
python3 -m reading_review compare --schools C1 C2 --dimension evidence
python3 -m reading_review trace --schools C1 C2 --dimension evidence --rubric R1
python3 -m reading_review audit --publication P1
python3 -m unittest discover -s tests -v
python3 -m examples.withdrawal_scenario --state var/scenario.json
```

改变状态的操作（分派、接受/退回/完成、撤回、新增评分或联系、替代裁决）必须显式
提供 `--state`；状态是追加式事件日志，重放后得到相同结果。启动控制台：

```sh
python3 -m reading_review --materials materials --state var/state.json serve
# 浏览器打开 http://127.0.0.1:8765/
```

控制台包含：总览、作品逐维状态、复核优先级、学校比较与追溯、资格撤回处理、
发布审计和操作记录。图表只用颜色区分两条学校均值；所有数值同时以表格给出。

## 业务口径

- **三种权威来源分离。** 原始评分只读；尺度调整是派生分析层，不改写原始分、
  不替代正式复核；逐维正式复核是独立记录，保留当时决定、日期、理由和原始评分
  引用。后来的校准资料不会静默推翻既有裁决；要改变结论只能显式替代
  （`supersede_adjudication`），旧记录标记 `superseded` 并保留。
- **逐维而非整篇。** `dimension_state` 对每个维度独立给出 `resolved` /
  `contested` / `unreviewed`。一个维度材料不足不会让同一作品的其它维度失效；
  比较只在确实成立的维度与规则内使用作品。
- **不重叠尺度。** 教师尺度由共同作品的二向模型估计，只有共同作品连通分量内的
  教师才被认为建立了尺度联系；孤立教师（数据中的 T11、T12）不会被当作已统一
  尺度。模型同时披露作品难易、教师尺度与余差三方方差占比和可区分教师数。
- **规则不混合。** 每个规则版本单独比较；跨规则换算必须同时满足统计充分且存在
  仍有效、覆盖共同作品的批准联系。数据中 L1（evidence）已撤回、L2（reflection）
  虽有效但相关性仅约 -0.07，因此两个维度都不能换算。
- **不确定性披露。** 学校均值按 `sample_weight` 加权，标准误按 household 聚类
  （兄弟姐妹跨校时不再当作独立观测），给出 95% 区间；区间含 0 时不允许宣称差异。
- **复核分派。** 教师不能复核自己首评、任教学校学生或已看过的作品；分派排序
  同时考虑分歧、已发布比较受影响程度、校准网络缺口与余量；接受后才占用容量，
  完成或接受的复核不因退回另一项而释放容量；同一作品不会重复确认给同一教师。
- **资格撤回。** 一次撤回分别处理三类后续：未完成分派撤销；已完成评分与正式
  结论保留原值但标注联系依据失效；已发布比较标记待复核。不会把所有评分重置为
  待评，也不会只重算均值后让失去资格的教师继续盲评。原始评分数据保持不变。

## 数据中已能验证的事实

- P1 声明只比较 C1/C2 的 evidence，实际引用 179 条评分，混入 C3–C6 的 119 条
  且包含另外两个维度，审计结论为引用无效。
- W72 的 evidence 已正式修分、reflection 被认定材料不足；同一作品因此同时具有
  可用维度与争议维度。
- L1 在 2026-07-20 撤回；L2 仍然有效但统计上不足以支持换算。
- T11、T12 没有共同作品评分，与其余十名教师不在同一连通分量。

## 代码结构

| 路径 | 作用 |
| --- | --- |
| `reading_review/bundle.py` | 读取与校验只读资料包 |
| `reading_review/domain.py` | 事件日志、权威来源、分派与撤回规则 |
| `reading_review/statlib.py` | 二向尺度模型、聚类标准误、区间与换算 |
| `reading_review/analysis.py` | 逐维状态、尺度网络、学校比较、优先级、发布审计 |
| `reading_review/reporting.py` | 追溯链与“行动是否改变比较”的逐步影响 |
| `reading_review/server.py`、`static/index.html` | 本地服务与控制台 |
| `examples/withdrawal_scenario.py` | 资格撤回 × 部分维度已裁定的可运行演练 |
| `tests/` | 资料检查与业务规则测试 |
