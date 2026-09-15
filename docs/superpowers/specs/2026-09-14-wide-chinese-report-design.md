# 宽屏中文日报与同日断点续跑设计

**日期：** 2026-09-14
**基线：** `origin/feat/feishu-signature` (`4aa3e22`)
**实现分支：** `feature/report-wide-cn`

## 目标

在远端 `feat/feishu-signature` 的 B+ 报告基础上做两项明确收敛：

1. 断点续跑只复用同一个 `report_date` 产生的 checkpoint。前复权历史数据不能跨交易日复用；隔天运行必须重新获取当日历史数据。
2. 保留卡片化、可展开的 B+ 信息架构，但扩大桌面内容区并将用户可见文案改为中文优先，使扫描结论更直观。

报告的 JSON、Markdown、扫描 artifact schema、排名算法和数据源选择不在本次范围内改变。

## 现状对比

### 远端 B+ 版本

- 摘要指标卡、共识卡片、Top 10 排名、11–30 原生展开区和自选股详情卡片已经具备。
- 首屏信息层级适合“快速扫盘 + 深度复核”。
- `details/summary` 不需要 JavaScript，适合静态 GitHub Pages。
- 现有 `.report-page` 最大宽度约为 72rem，宽屏下左右留白偏多。
- 页面标签大部分为英文或中英混排，用户需要额外解释 Coverage、Consensus、Valid 等含义。

### 本地旧版本

- 使用宽表格承载完整字段，审计信息完整但首屏密度高。
- 移动端需要横向滚动，代码、名称、评分、证据和风险难以同时阅读。
- 不作为本次布局基线，只保留其“完整字段可审计”的优点。

## 设计方案

### 1. 同日断点续跑

Checkpoint 继续按以下路径隔离：

```text
market-scans/<report_date>/progress.json
```

每个 checkpoint 增加 `execution_date` 字段，表示扫描开始时捕获的
Asia/Shanghai 自然日；该字段的加入使 checkpoint schema 从 v1 升级到
v2。v2 的固定字段为：

```text
schema_version: integer literal 2
report_date: YYYY-MM-DD
execution_date: YYYY-MM-DD
rule_version: "market-scan-v1"
scoring_config_hash: 64-char lowercase SHA-256
manifest_hash: 64-char lowercase SHA-256
source_revision: string
universe_quotes: normalized quote array
completed: serialized candidate-result array
state: "running" | "complete"
updated_at: timezone-aware UTC datetime
last_batch: non-negative integer
```

除 `source_revision` 外，以上字段均为必需字段。无法提供
`execution_date` 的 v1 checkpoint 不得被自动推断或复用，应报错提示需要
人工归档后重新扫描。恢复必须同时满足：

- checkpoint 的 `report_date` 等于本次请求的 `report_date`；
- checkpoint 的 `execution_date` 等于本次运行的 Asia/Shanghai 自然日；
- universe manifest hash 一致；
- scoring config hash 一致；
- checkpoint schema 和数据校验通过。

前复权历史结果、候选状态和评分只允许在同一自然日、同一
`report_date` 内复用。即使手工传入同一个旧的 `report_date`，跨过
Asia/Shanghai 午夜后也必须重新拉取。不同日期永远不读取旧日期
checkpoint；系统启动当日的新扫描，并保留旧日期 checkpoint 供审计。

当一次受支持的运行在当前 `report_date` 目录中发现已过期或配置已变化的
`progress.json` 时，系统先将
它原子地移到同目录的
`progress-archive/<execution_date>-<updated_at>.json`，再启动全新扫描；
不删除旧 checkpoint，也不把新结果追加到旧结果中。这里的
`updated_at` 使用 checkpoint 中的 UTC 时间，格式固定为
`YYYYMMDDTHHMMSSZ`；`execution_date` 使用 `YYYY-MM-DD`。

损坏、无法解析、schema 不支持、manifest 自校验 hash mismatch 的
checkpoint 属于完整性错误：不得自动归档或静默恢复，应报错并要求人工
处理。`report_date`/`execution_date` 过期和 scoring config hash 变化属于
可识别的陈旧状态：可以归档并启动全新扫描。

扫描开始时冻结 `execution_date`。每个批次开始前和最终 artifact 生成前
都检查当前 Asia/Shanghai 日期；如果跨过午夜，停止本次运行，不生成
artifact，并只保留带有原 `execution_date` 的 running checkpoint。次日的
正常运行使用新的 `report_date` 目录，不扫描旧目录、不归档旧 checkpoint；
旧 checkpoint 保留供审计。只有在一个受支持的运行明确命中同一
`report_date` 目录、且该日期仍是当前 Asia/Shanghai 日期时，才执行上面的
陈旧 checkpoint 归档规则。

同日恢复继续使用 checkpoint 保存的 universe snapshot，避免恢复时重新获取一份可能已经变化的股票池。最终 artifact 仍只在所有 eligible candidate 完成后生成。

`scoring_config_hash` 是用于恢复安全校验的语义配置 hash，覆盖评分、
过滤、历史窗口和 eligibility 所需的配置；它可以排除只影响并发或批大小
的运行时参数。它与最终 `scan.json` 的完整 `config_hash` 分开保存：
后者仍用于 artifact provenance 和最终报告一致性校验。

Checkpoint 的生命周期和优先级：

- 没有 checkpoint：获取一次当前 universe，写入 `state=running` 的
  checkpoint，开始批处理。
- `state=running` 且日期、hash、schema 均匹配：从已完成候选继续。
- `state=complete` 但没有 `scan.json`：使用 checkpoint 中的 snapshot 和
  completed results 生成 artifact。
- `scan.json` 已存在且通过 config/input 校验：immutable artifact 优先，
  不重复扫描；遗留 progress 文件只用于审计。
- 过期或 scoring hash 变化：按上面的保留旧文件并全新扫描规则处理；
  完整性错误则停止并保留原文件。

manifest hash 是 checkpoint 内保存的完整、规范化 universe snapshot 的
自校验 hash。同日恢复不重新调用 live universe provider；hash mismatch
表示 checkpoint 损坏或不完整，应报错并要求人工处理。

扫描 CLI 的现有日期保护继续保留：没有有效 immutable artifact 时，
resumable live scan 只允许当前 Asia/Shanghai 市场日期。手工指定旧的
`report_date` 不会触发历史数据复用；若需要补历史报告，应使用已有
snapshot/artifact 的离线生成流程，而不是 resumable live scan。

`progress.json` 与 `scan.json` 的处理矩阵如下：

| `scan.json` | `progress.json` | 行为 |
| --- | --- | --- |
| 有且校验通过 | 任意状态 | immutable artifact 优先，不重复扫描 |
| 有但解析、schema 或 config/input 校验失败 | 任意状态 | 报错，不能绕过 artifact 改用 checkpoint |
| 无 | 无 | 获取当前 universe，创建 v2 running checkpoint |
| 无 | running 且同日/hash 有效 | 继续未完成候选 |
| 无 | complete 且同日/hash 有效 | 从 checkpoint 生成 artifact |
| 无 | 过期或 scoring hash 变化 | 归档 checkpoint，启动全新扫描 |
| 无 | 损坏、schema 不支持或 manifest hash mismatch | 报错，人工处理，不覆盖原文件 |

checkpoint 在最终 `scan.json` 原子写入成功前必须保持 `state=running`；
只有 artifact 写入成功后才允许写入 `state=complete`。跨午夜、批处理异常
或 artifact 写入失败都不能把 checkpoint 标记为 complete。

### 2. 桌面宽度和响应式行为

报告页面采用“背景铺满、内容受限”的布局：

- `.report-page` 在桌面端使用 `width: min(94vw, 1320px)`；
- 取消旧的 72rem 窄上限，但保留最大宽度，避免超宽屏长行；
- 现有卡片、排名行和详情字段继续使用 `min-width: 0` 与 `overflow-wrap: anywhere`；
- 640px 以下继续单列指标、共识卡和自选股卡片；
- 375px 下不依赖横向滚动，排名行改为多行网格，评分保持右侧对齐；
- `site/index.html` 的共享布局不受 `.report-page` 规则影响。

### 3. 中文优先信息架构

HTML 用户可见文案统一使用中文，必要时保留简短英文作为辅助。页面
`lang` 改为 `zh-CN`，标题、返回链接、H1、状态徽章、运行时元数据、
空状态、不可用状态、详情 summary 和 watchlist 分组标题也纳入翻译：

| 当前概念 | 中文显示 |
| --- | --- |
| Market summary | 市场摘要 |
| Coverage | 扫描覆盖率 |
| Valid / eligible | 有效股票 / 候选股票 |
| Universe | 股票池总数 |
| Consensus count | 多策略共识 |
| Trend Top 10 | 趋势策略 Top 10 |
| Balanced Top 10 | 均衡策略 Top 10 |
| View full ranking | 查看第 11–30 名 |
| Watchlist | 自选股跟踪 |
| Quality and audit | 数据质量与审计 |
| All metrics | 全部指标 |
| Structure details | 结构分析详情 |
| Signals and levels | 信号、风险与关键价位 |
| All dated reports | 返回报告列表 |
| Runtime metadata | 运行时元数据 |
| Full-market rankings | 全市场排名 |
| No exact intersection | 本次扫描没有多策略交集 |
| Full-market rankings unavailable | 全市场排名不可用 |

排名行首屏保留：排名、代码、名称、评分、是否双策略共识、简短风险提示。组件分数、证据、风险、交易日、数据源等完整字段继续放入详情展开区。

运行时元数据、失败原因和 unavailable/no-consensus 状态继续完整保留，只翻译标签，不改变原始代码和值。

## 不变项

- `ReportDocument`、`MarketRankings`、扫描 artifact 和 JSON schema 不变。
- Markdown 输出保持原有内容和格式，避免下游审计链接失效。
- 排名排序、共识交集、覆盖率门槛和数据源 fallback 不变。
- HTML 动态内容继续统一经过 escaping。
- 不引入 JavaScript；继续使用原生 `details/summary`。
- 不把报告生成 artifact 纳入代码提交。

## 错误处理

- checkpoint 读取、校验或持久化失败时显式报错，不静默从损坏状态继续。
- checkpoint 日期或 scoring hash 不匹配时，按“需要全新扫描”处理并保留旧文件；
  manifest hash mismatch、损坏或不支持的 schema 则停止并保留原文件。
- 报告 unavailable、扫描失败或无共识时继续渲染明确状态，不伪造排名。
- 不增加宽泛异常捕获或成功形状 fallback。

## 验证

新增或调整测试覆盖：

1. 同日 checkpoint 恢复时不重复获取已完成候选历史；
2. 同一 `report_date` 但跨 Asia/Shanghai 午夜时不复用旧 checkpoint，
   旧目录中的 running 文件保留且不被次日运行读取；
3. 不同 `report_date` 不复用旧 checkpoint，且旧 checkpoint 保留；
4. checkpoint 的 scoring hash mismatch 按过期归档策略处理；manifest hash
   mismatch、损坏和不支持的 schema 显式报错，不静默成功；
5. `progress.json` 与 `scan.json` 共存时 immutable artifact 优先，且
   完整 checkpoint 可生成缺失的 artifact；
6. 中文页面的 `lang`、标题、所有固定标签、状态和详情 summary 均通过
   HTML contract assertions 验证，数据值和技术代码不被错误翻译；
7. 桌面宽度规则只作用于 `.report-page`，内容宽度为 `min(94vw, 1320px)`；
8. 在现有浏览器 companion 的 375px viewport 中，使用浏览器控制台验证
`document.documentElement.scrollWidth <=
   document.documentElement.clientWidth`，且 `.ranking-row` 和
   `.detail-field` 的右边界不超过 viewport；
9. Markdown、JSON 和 scan artifact 序列化不变；
10. 全量 pytest、Ruff 和 `git diff --check` 通过。
