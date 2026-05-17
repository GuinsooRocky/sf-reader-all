---
name: archive
description: 把一组网页批量存成「带 CSS 的 1:1 自包含 HTML」并生成索引页 —— 适用于付费小册、整套在线文档、技术教程、或你随手攒的一堆链接；每篇是单文件 HTML（CSS/图片全内联），任意浏览器双击即开，可离线、可暗黑。触发词：「归档」、「批量存网页」、「把这些页面存下来」、「1:1 存档」、「离线收藏」、「下载整本」、「下载整套文档」、「archive」、「/archive」。靠 sf-reader-all 的 `archive-links` + `archive` 两个命令，禁止用 chrome MCP。
---

# Archive Skill

> 一组网页 → 一批 1:1 自包含 HTML + 索引页

## 何时使用

用户想把**多个页面原样离线存下来**时触发，典型说法：「归档」、「批量存网页」、
「把这些页面存下来」、「1:1 存档」、「离线收藏」、「下载整本」、「下载整套文档」、
`/archive`。常见对象：

- 付费小册 / 在线课程的全部章节
- 一整套在线文档（docs 站、知识库）
- 你手上攒的一批文章链接

不限于「小册」—— 任何「多页面原样离线」的需求都走这里。

## 工作流（3 步）

引擎只认一个 **URL 列表**。「该归档哪些 URL」由本流程里**你和我一起判断**，
不写死在代码里。

### Step 1 — 采集链接

对目标的目录页 / 索引页 / 侧边栏页跑：

```bash
sf-reader-all archive-links <目录页URL> [--session NAME] --out raw-links.txt
```

它把该页**所有同源链接**（`url | 链接文字`）原样吐出来，不做任何过滤。
目标在登录墙后面时**必须**加 `--session`（见下文登录态）。

> 多级目录（目录页 → 分章页 → 文章）：对每个分章页各跑一次，合并结果。

### Step 2 — 我筛选成 `urls.txt`

读 Step 1 的原始链接，**由我判断**哪些是真正要的内容页、砍掉导航/页脚/重复，
排序，必要时分章，产出 `urls.txt`：

```
## 第一章 标题            ← ## 开头 = 索引里的分章（可选）
https://site.com/a/       ← 一行一个 URL
https://site.com/b/ | 自定义标题   ← URL 后跟 ` | 标题` 可覆盖（不写则用页面 <title>）
# 这行是注释，会被忽略
```

筛选标准随内容类型变 —— 小册按章节、docs 按目录树、散链直接列。
这一步的灵活性全在这里，引擎不需要改。

### Step 3 — 跑归档

```bash
sf-reader-all archive urls.txt [--out DIR] [--theme dark|light] [--concurrency N]
```

逐篇 MHTML 快照 → 转自包含 HTML → 写 `index.html` + `manifest.json`。
- 默认输出 `<urls文件名>-archive/`，默认暗黑主题。
- **增量**：已转好的 `NNN-*.html` 自动跳过 —— 失败后重跑只补缺的。
- 看 `manifest.json` 的 `status` 字段确认成功/失败。

## 登录态（付费 / 登录墙内容）

目标需要登录或订阅时，**必须**先跑一次：

```bash
sf-reader-all archive --login <任意目标页URL>
```

弹可见浏览器，你手动登录 / 解锁，**关窗即存** session（按域名命名）。
之后 `archive-links` 和 `archive` 都用 `--session <域名>` 复用它。
扫码这步**只能**你本人做，skill 和我都替不了。

## 可选项

- `--theme dark|light` — 输出主题，默认 `dark`（仅对 class 式主题站生效）
- `--concurrency N` — 并发抓取数，默认 5
- `--strip-pattern REGEX` — 从每篇 HTML 删掉匹配片段（如个人头像链接），可重复
- `--session NAME` — 复用某个登录态（域名 / 名字 / 路径）

## 示例输出

`archive` 跑完，输出目录形如：

```
booklet-archive/
├── index.html          ← 暗黑索引，按 ## 分章列出全部条目
├── manifest.json       ← 机器可读清单：index/url/title/file/status/error
├── 001-第一篇标题.html   ← 单文件，CSS/图片内联，双击即开
├── 002-第二篇标题.html
└── ...
```

`manifest.json` 单条形如：

```json
{ "index": 1, "section": "第一章", "url": "https://site.com/a/",
  "title": "第一篇标题", "file": "001-第一篇标题.html", "status": "ok" }
```

## 反模式 / 已知坑

1. **不要**用 chrome MCP —— 这 3 步全部走 sf-reader-all 自带的 Playwright。
2. **不要**在代码里写站点专属的目录发现逻辑 —— 筛 URL 永远是 Step 2 我的人工判断。
3. `<script>` **会被剥掉** —— SPA 站保留 JS 会 hydration 把预渲染内容刷白；
   快照只要静态视觉 1:1。
4. **字体若未进快照**，离线打开会回退系统字体，布局/颜色/图片不受影响。
5. 视觉是否 1:1 我**无法**自己确认 —— 批量跑完**必须**让用户挑一篇打开核对。
