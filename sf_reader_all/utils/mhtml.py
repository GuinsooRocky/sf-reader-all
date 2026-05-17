# -*- coding: utf-8 -*-
"""MHTML -> self-contained single-file HTML.

Parses a Chrome MHTML snapshot, inlines every resource (CSS as <style>,
images/fonts as data: URIs), strips <script> (SPA sites would otherwise
hydrate and blank the pre-rendered content), and optionally switches the
colour theme.

Extracted from the standalone booklet exporter so the `archive` command
can reuse it for any MHTML snapshot.
"""

import base64
import email
import re
from pathlib import Path


def _to_data_uri(ctype: str, data: bytes) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{ctype};base64,{b64}"


def mhtml_to_selfcontained(mhtml_path, out_path, *, theme="dark",
                           strip_patterns=()) -> int:
    """Convert one .mhtml file to a self-contained .html file.

    Args:
        mhtml_path: source .mhtml path.
        out_path:   destination .html path (parent dirs are created).
        theme:      "dark", "light" or None. "dark"/"light" rewrites the
                    root <html class="..."> and `color-scheme` when the
                    page uses a class-based theme; None leaves it as-is.
        strip_patterns: iterable of regex strings; each match is removed
                    from the HTML (re.S | re.I). Use for site chrome such
                    as a personal avatar link.

    Returns:
        Size of the written file in bytes.
    """
    with open(mhtml_path, "rb") as f:
        msg = email.message_from_binary_file(f)

    main = None
    resources = {}   # Content-Location -> data: URI (images, fonts, ...)
    css_parts = {}   # Content-Location -> css text

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        location = part.get("Content-Location", "")
        data = part.get_payload(decode=True)
        if data is None:
            continue
        if ctype == "text/html" and main is None:
            main = data.decode("utf-8", "replace")
        elif ctype == "text/css":
            css_parts[location] = data.decode("utf-8", "replace")
        else:
            resources[location] = _to_data_uri(ctype, data)

    if main is None:
        raise RuntimeError(f"no main HTML part in MHTML: {mhtml_path}")

    html = main

    # Strip <script> — pre-rendered SPA content would otherwise be
    # hydrated away and blanked.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<script\b[^>]*/>", "", html, flags=re.I)

    # Inline CSS: swap each <link rel=stylesheet> for a <style> block,
    # rewriting url() references inside the CSS to data: URIs.
    def _rewrite_css(css: str) -> str:
        for url, uri in resources.items():
            if url and url in css:
                css = css.replace(url, uri)
        return css

    for url, css in css_parts.items():
        if not url:
            continue
        style = "<style>\n" + _rewrite_css(css) + "\n</style>"
        link_re = re.compile(
            r'<link\b[^>]*href="' + re.escape(url) + r'"[^>]*>', re.I)
        if link_re.search(html):
            html = link_re.sub(lambda m: style, html, count=1)

    # Inline remaining binary resources (images, fonts) as data: URIs.
    for url, uri in resources.items():
        if url and url in html:
            html = html.replace(url, uri)

    # Caller-supplied removals (e.g. a personal avatar link).
    for pattern in strip_patterns:
        html = re.sub(pattern, "", html, flags=re.S | re.I)

    # Theme: rewrite a class-based light/dark root if the page has one.
    if theme in ("dark", "light"):
        other = "light" if theme == "dark" else "dark"
        html = re.sub(
            r'(<html\b[^>]*\bclass=")' + other + r'(")',
            r"\g<1>" + theme + r"\g<2>", html, count=1, flags=re.I)
        html = html.replace(f"color-scheme: {other}",
                            f"color-scheme: {theme}", 1)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out.stat().st_size
