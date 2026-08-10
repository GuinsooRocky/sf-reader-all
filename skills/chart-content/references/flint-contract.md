# Flint 输入约定

本 Skill 使用 `flint-chart-mcp` 0.5.x。Flint 当前通过 npm/MCP 提供，尚无正式 Python 包。

## 最小输入

```json
{
  "data": { "url": "projects.data.json" },
  "semantic_types": {
    "project": "Name",
    "stars": "Count"
  },
  "chart_spec": {
    "chartType": "Bar Chart",
    "title": "项目 A 的 Star 数领先",
    "subtitle": "GitHub Star 数，核验于 2026-08-11",
    "encodings": {
      "x": { "field": "project" },
      "y": { "field": "stars" }
    },
    "chartProperties": { "showValueLabels": true }
  },
  "theme_spec": "datawrapper"
}
```

`data` 只能二选一：小数据使用 `{ "values": [...] }`，本地 JSON/CSV/TSV 使用 `{ "url": "..." }`。远程 URL 不会被 Flint 读取。

## 内容分析常用图表

- 单一指标横向比较：`Bar Chart`
- 两组指标并排比较：`Grouped Bar Chart`，第二分类使用 `group`
- 组成占比：`Stacked Bar Chart`；类别很少时才用 `Pie Chart`
- 时间趋势：`Line Chart`
- 两个数值变量的关系：`Scatter Plot`
- 二维密度或交叉矩阵：`Heatmap`

使用精确的图表名称。需要其他图表时通过 Flint MCP 的 `list_chart_types` 查询，不要猜名称。

## 常用语义类型

- 日期时间：`Date`、`DateTime`、`YearMonth`
- 数值：`Count`、`Quantity`、`Amount`、`Price`、`Percentage`
- 有方向的数值：`Profit`、`PercentageChange`、`Sentiment`
- 离散字段：`Name`、`Category`、`Status`、`Rank`、`Score`
- 地理字段：`Country`、`State`、`City`、`Region`

每个被编码的字段都必须出现在 `semantic_types`。百分比值是 0–1 还是 0–100，要先查看真实数据后再决定，不能重复缩放。

## 编码规则

- `Bar Chart`：一个离散轴和一个数值轴。
- `Grouped Bar Chart`：第二分类必须放在 `group`，不是 `color`。
- `Stacked Bar Chart`：组成分类放在 `color`。
- `Pie Chart`：数值放在 `size`，类别放在 `color`。
- 多列折线可以在 `x` 或 `y` 使用字段数组，例如 `"y": ["sales", "profit"]`；除此之外的数据转换都应在 Flint 之前完成。

标题应表达发现，副标题应说明测量对象、时间、指标和单位。样式默认交给主题；只有用户明确要求时才修改颜色、字体或后端输出。

## 渲染接口

静态产物使用 MCP 工具 `render_chart`：

- `backend`: `vegalite`、`echarts` 或 `chartjs`
- `format`: `svg` 或 `png`；Chart.js 仅支持 PNG

本 Skill 的 `scripts/render_chart.py` 会处理 MCP 初始化、调用和文件落盘。默认包版本是 `flint-chart-mcp@0.5.0`；需要升级测试时可通过环境变量 `FLINT_CHART_MCP_PACKAGE` 覆盖。
