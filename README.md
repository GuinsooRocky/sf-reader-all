# sf-reader-all

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Universal content reader — fetch, parse, transcribe, and digest content from URLs or local documents.

Give it a URL (article, video, podcast, tweet) or a local office document, get back structured content. Works as CLI, Python library, MCP server, or Claude Code skills.

**简体中文：** [README.zh-CN.md](./README.zh-CN.md)

## What It Does

```
URL / Local Document → Source Detection → Read Content → Unified Output
                              ↓                 ↓
                         auto-detect       text: Jina Reader
                         7+ platforms      document: anydoc (optional)
                                           video: yt-dlp subtitles
                                           audio: Whisper transcription
                                           API: Bilibili / RSS / Telegram
```

The Python layer handles text fetching and YouTube subtitle extraction. The **Claude Code skills** (optional) add full Whisper transcription for video/podcast and AI-powered content analysis.

## Three Layers

sf-reader-all is composable. Use the layers you need:

| Layer | What | Format | Install |
|-------|------|--------|---------|
| **Python CLI/Library** | Basic content fetching + unified schema | See [Install](#install) | Required |
| **Claude Code Skills** | Video transcription + AI analysis | Copy `skills/` to `~/.claude/skills/` | Optional |
| **MCP Server** | Expose reading as MCP tools | `python mcp_server.py` | Optional |

Batch reads keep at most 16 Sources active, offload blocking libraries and subprocesses to a shared eight-worker pool, and cap a shared Browser Runtime at six pages. Successful Content Items cross the persistence boundary once: one inbox save and one Markdown append per batch.

### Layer 1: Python CLI

```bash
# Fetch any URL
sf-reader-all https://mp.weixin.qq.com/s/abc123

# Fetch a tweet
sf-reader-all https://x.com/elonmusk/status/123456

# Read a local office document (requires the documents extra)
sf-reader-all ./report.docx

# Fetch multiple URLs
sf-reader-all https://url1.com https://url2.com

# Login to a platform (one-time, for browser fallback)
sf-reader-all login xhs

# View inbox
sf-reader-all list
```

### Layer 2: Claude Code Skills

> Requires cloning the repo (not included in pip install).

For video/podcast transcription, content analysis, and optional data charts:

```
skills/
├── video/          # YouTube/Bilibili/podcast → full transcript via Whisper
├── analyzer/       # Any content → structured analysis report
└── chart-content/  # Verified content data → Flint SVG/PNG chart
```

Install:
```bash
cp -r skills/video ~/.claude/skills/video
cp -r skills/analyzer ~/.claude/skills/analyzer
cp -r skills/chart-content ~/.claude/skills/chart-content
```

Then in Claude Code, just send a YouTube/Bilibili/podcast link — the video skill auto-triggers and produces a full transcript + summary. When an analysis contains enough reliable, comparable data, `chart-content` can generate a sourced Flint chart without changing the Python reader's default dependencies. Chart rendering requires Node.js 18+ and downloads the pinned `flint-chart-mcp` package through `npx` on first use.

### Layer 3: MCP Server

> Requires cloning the repo (mcp_server.py is not included in pip install).

```bash
git clone https://github.com/GuinsooRocky/sf-reader-all.git
cd sf-reader-all
pip install -e ".[mcp]"
python mcp_server.py
```

Tools exposed:
- `read_url(url)` — fetch any URL
- `read_batch(urls)` — fetch multiple URLs concurrently
- `list_inbox()` — view previously fetched content
- `detect_platform(url)` — identify platform from URL

Claude Code config (`~/.claude/claude_desktop_config.json`):
```json
{
    "mcpServers": {
        "sf-reader-all": {
            "command": "python",
            "args": ["/path/to/sf-reader-all/mcp_server.py"]
        }
    }
}
```

## Supported Platforms

| Platform | Text Fetch | Video/Audio Transcript |
|----------|-----------|----------------------|
| YouTube | ✅ Jina | ✅ yt-dlp subtitles → Groq Whisper fallback |
| Bilibili (B站) | ✅ API | ✅ via Claude Code skill |
| X / Twitter | ✅ Jina → Playwright | — |
| WeChat (微信公众号) | ✅ Playwright (stealth) | — |
| Xiaohongshu (小红书) | ✅ Jina → Playwright* | — |
| Telegram | ✅ Telethon | — |
| RSS | ✅ feedparser | — |
| 小宇宙 (Xiaoyuzhou) | — | ✅ via Claude Code skill |
| Apple Podcasts | — | ✅ via Claude Code skill |
| Any web page | ✅ Jina fallback | — |
| Local documents (Word, PowerPoint, Excel, PDF, EPUB, CSV) | ✅ anydoc* | — |

> \*XHS requires a one-time login: `sf-reader-all login xhs` (saves session for Playwright fallback)
>
> Local documents require the optional `documents` dependency. Image-only PDFs are not supported because local anydoc parsing does not include OCR.
>
> YouTube Whisper transcription requires `GROQ_API_KEY` — get a free key from [Groq](https://console.groq.com/keys)

## Install

```bash
# From GitHub (recommended)
pip install git+https://github.com/GuinsooRocky/sf-reader-all.git

# With Telegram support
pip install "sf-reader-all[telegram] @ git+https://github.com/GuinsooRocky/sf-reader-all.git"

# With browser fallback (Playwright — for XHS/WeChat anti-scraping)
pip install "sf-reader-all[browser] @ git+https://github.com/GuinsooRocky/sf-reader-all.git"
playwright install chromium

# With local office document support (anydoc)
pip install "sf-reader-all[documents] @ git+https://github.com/GuinsooRocky/sf-reader-all.git"

# With all optional dependencies
pip install "sf-reader-all[all] @ git+https://github.com/GuinsooRocky/sf-reader-all.git"
playwright install chromium
```

Or clone and install locally:
```bash
git clone https://github.com/GuinsooRocky/sf-reader-all.git
cd sf-reader-all
pip install -e ".[all]"
playwright install chromium
```

### Dependencies for video/audio (optional)

```bash
# macOS
brew install yt-dlp ffmpeg

# Linux
pip install yt-dlp
apt install ffmpeg
```

For Whisper transcription, get a free API key from [Groq](https://console.groq.com/keys) and set:
```bash
export GROQ_API_KEY=your_key_here
```

## Use as Library

```python
import asyncio
from sf_reader_all.reader import UniversalReader

async def main():
    reader = UniversalReader()
    content = await reader.read("https://mp.weixin.qq.com/s/abc123")
    print(content.title)
    print(content.content[:200])

asyncio.run(main())
```

After installing the `documents` extra, the Python library reads local files through `read_file`:

```python
content = await reader.read_file("./report.docx")
```

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_API_ID` | Telegram only | From https://my.telegram.org |
| `TG_API_HASH` | Telegram only | From https://my.telegram.org |
| `GROQ_API_KEY` | Whisper only | From https://console.groq.com/keys (free) |
| `INBOX_FILE` | No | Path to inbox JSON (default: `./unified_inbox.json`) |
| `OUTPUT_DIR` | No | Directory for Markdown output (default: disabled) |
| `OBSIDIAN_VAULT` | No | Path to Obsidian vault (writes to `01-收集箱/sf-reader-all-inbox.md`) |

## Architecture

```
sf-reader-all/
├── sf_reader_all/              # Python package
│   ├── cli.py             # CLI entry point
│   ├── reader.py          # URL dispatcher (UniversalReader)
│   ├── schema.py          # Unified data model (UnifiedContent + Inbox)
│   ├── login.py           # Browser login manager (saves sessions)
│   ├── fetchers/
│   │   ├── browser_runtime.py # Reusable Playwright browser/context lifecycle
│   │   ├── jina.py        # Jina Reader (universal fallback)
│   │   ├── browser.py     # Playwright headless (anti-scraping fallback)
│   │   ├── bilibili.py    # Bilibili API
│   │   ├── youtube.py     # yt-dlp subtitle extraction
│   │   ├── rss.py         # feedparser
│   │   ├── telegram.py    # Telethon
│   │   ├── twitter.py     # Jina-based
│   │   ├── wechat.py      # Playwright stealth (Jina always gets the CAPTCHA stub)
│   │   └── xhs.py         # Jina → Playwright + session fallback
│   ├── parsers/
│   │   └── document.py    # Optional anydoc adapter for local files
│   └── utils/
│       ├── async_runtime.py # Bounded blocking-I/O worker pool
│       └── storage.py     # Batched inbox + Markdown output
├── skills/                # Claude Code skills
│   ├── video/             # Video/podcast → transcript + summary
│   └── analyzer/          # Content → structured analysis
├── mcp_server.py          # MCP server entry point
└── pyproject.toml
```

## How the Layers Work Together

```
User sends URL
    │
    ├─ Text content (article, tweet, WeChat)
    │   └─ Python fetcher → UnifiedContent → inbox
    │
    ├─ Video (YouTube, Bilibili, X video)
    │   ├─ Python fetcher → metadata (title, description)
    │   └─ Video skill → full transcript via subtitles/Whisper
    │
    ├─ Podcast (小宇宙, Apple Podcasts)
    │   └─ Video skill → full transcript via Whisper
    │
    └─ Analysis requested
        └─ Analyzer skill → structured report + action items

User sends local document
    └─ anydoc adapter → Markdown → UnifiedContent → inbox
```

Archive manifests record navigation, content-settle, snapshot, MHTML conversion, queue, and total timings. These measurements are the gate for moving MHTML conversion to Rust; the project does not assume a rewrite will be faster without profile evidence.

## License

MIT
