---
name: chart-content
description: 将 sf-reader-all 抓取的文章、视频转录、批量内容或后台数据变成有来源、可复现的 Flint 数据图表。用户要求“画图”“图表化”“数据对比”“趋势图”“给分析配图”，或 analyzer 识别出至少三项可比数据时使用。先核验数据与单位，再生成 ChartAssemblyInput；能运行 Flint 时导出 SVG/PNG，不能运行时保留数据和规格，不阻塞正文分析。
---

# 内容数据图表

把 read-all 的内容分析扩展成可追溯的数据图表。只画来源可靠、单位一致、确实能帮助理解的数据，不为了装饰强行画图。

开始前完整阅读 [Flint 输入约定](references/flint-contract.md)。运行脚本时始终从本文件所在目录解析 `scripts/render_chart.py`，不要假设 Skill 安装在固定路径。

## 工作流

### 1. 取得正文

已有 `UnifiedContent`、转录稿或后台数据时直接使用。只有 URL 时先运行：

```bash
sf-reader-all --json '<url>'
```

不要让图表步骤重新实现微信、小红书、X、B 站或 YouTube 的抓取。

### 2. 经过画图闸门

仅在同时满足以下条件时继续：

- 至少有 3 个可比较的数据点，或有一条有效时间序列。
- 指标口径和单位一致；百分比要确认是 0–1 还是 0–100。
- 每个数据点能追溯到原文、用户文件或核验过的第一方来源。
- 图表能揭示排序、趋势、差距、分布或相关性，而不只是重复一句话。

以下情况不画：

- 数字只是案例编号、版本号、年份罗列或营销话术。
- 缺少单位、时间范围或比较对象。
- 把总计与分项混在同一个堆叠、分组或着色维度中。
- 需要猜测、补齐或外推原文没有的数据。

Star、价格、排名、阅读量等会变化的数据，必须先查当前第一方来源，并记录核验时间。无法核验时标成“原文声称”，不要伪装成当前事实。

### 3. 准备数据

优先在分析输出旁创建 `charts/<内容ID或短标题>/`，保存：

```text
<slug>.data.json     原始制图行
<slug>.chart.json    Flint ChartAssemblyInput
<slug>.svg           默认静态图
```

数据行使用稳定、短小的字段名，并保留不参与编码的溯源字段：

```json
[
  {
    "project": "示例项目",
    "stars": 3200,
    "source_url": "https://github.com/example/project",
    "observed_at": "2026-08-11"
  }
]
```

需要聚合、过滤、派生比例、连接或宽长表转换时，先在宿主工具中完成，再交给 Flint。Flint 是图表编译器，不是数据清洗器。

### 4. 编写并校验规格

让 `data.url` 指向同目录的数据文件；小于几十行的数据也可以放进 `data.values`。标题写结论，副标题写对象、时间范围、指标和单位。

默认使用 Vega-Lite 后端和 `datawrapper` 主题。只有用户指定或明显属于研究论文、商业汇报等场景时再换主题。

先做本地结构校验：

```bash
python3 <skill-dir>/scripts/render_chart.py <slug>.chart.json \
  --validate-only --require-provenance
```

校验失败时修改数据或规格，不要绕过校验。

### 5. 渲染

默认导出 SVG，便于文章、HTML 和后续编辑：

```bash
python3 <skill-dir>/scripts/render_chart.py <slug>.chart.json \
  --output <slug>.svg --require-provenance
```

需要位图时将输出后缀改成 `.png`。脚本默认通过 `npx` 运行固定版本的 `flint-chart-mcp`，不改全局 MCP 配置。首次执行可能需要下载 npm 包。

如果 `npx`、网络或 Flint 渲染不可用：

1. 保留 `.data.json` 和 `.chart.json`。
2. 报告静态图片未生成的具体原因。
3. 继续完成正文分析，不把图表失败冒充内容抓取失败。

### 6. 返回结果

在分析报告里只新增一个简短的“数据图表”小节，包含：

- 图表文件链接或预览。
- 一句话结论。
- 数据来源和核验时间。
- 必要的口径限制。

不要只返回一张没有解释和来源的图。

## 质量底线

- 不发明字段、数字、单位、时间或来源。
- 不直接手写 ECharts、Vega-Lite 或 Chart.js 输出规格；先写 Flint 输入。
- 不把大数据集整段内嵌进对话或规格。
- 不把图表产物写回 `UnifiedContent.content`；路径可在后续流程中记录到现有 `extra` 字段。
- 图表只是 analyzer 的可选产物，不能改变 `sf-reader-all <url>` 的默认行为。
