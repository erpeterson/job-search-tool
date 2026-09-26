"""Minimal, escaping Markdown-to-HTML renderer for packet previews."""

import re
from html import escape

_SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:")


def escape_html(value):
    return escape(str(value or ""), quote=True)


def _link(match):
    label, href = match.group(1), match.group(2)
    if not href.lower().startswith(_SAFE_LINK_SCHEMES):
        return label
    return f'<a href="{href}" target="_blank" rel="noopener">{label}</a>'


def render_inline_markdown(text):
    inline = escape_html(text)
    inline = re.sub(r"`([^`]+)`", r"<code>\1</code>", inline)
    inline = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", inline)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, inline)


def markdown_to_html(markdown):
    html = []
    in_list = False
    in_code = False
    code_lines = []

    def close_list():
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                html.append(f"<pre><code>{escape_html(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            html.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            continue
        if stripped == "---":
            close_list()
            html.append("<hr>")
            continue
        bullet = re.match(r"^-\s+(.+)$", stripped)
        if bullet:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{render_inline_markdown(bullet.group(1))}</li>")
            continue
        close_list()
        html.append(f"<p>{render_inline_markdown(stripped)}</p>")
    if in_code:
        html.append(f"<pre><code>{escape_html(chr(10).join(code_lines))}</code></pre>")
    close_list()
    return "\n".join(html)
