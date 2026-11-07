"""Build the Ddove1 site from content/ into _site/.

content/
  til/<topic>/*.md   things learned, grouped by topic
  tif/*.md           things found
  writeups/*.md      write-ups at the end of a book or a few chapters
  pages/*.md         standalone pages (about)

Features: [[wikilinks]] by file name (unpublished targets become plain text),
backlinks, per-section index pages, Atom feeds, and TeX math rendered at build
time to MathML (via KaTeX) and drawn with the Euler font.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

ROOT = Path(__file__).parent
CONTENT = ROOT / "content"
STATIC = ROOT / "static"
OUT = ROOT / "_site"

SITE_TITLE = "Ddove1"
SITE_URL = "https://divingdove1.github.io"
CSS_VERSION = "1"  # bump when static/style.css changes, so browsers drop the cached copy

SECTIONS = {
    "til": "TIL",
    "tif": "TIF",
    "writeups": "結案",
}


# ---------------------------------------------------------------- notes

@dataclass
class Note:
    path: Path
    kind: str  # til | tif | writeups | pages
    stem: str  # file name without .md: the wikilink key
    meta: dict
    title: str
    body: str  # markdown without front matter and title line
    date: dt.date
    topic: str | None = None
    backlinks: list["Note"] = field(default_factory=list)
    html: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.meta.get("slug") or self.stem)

    @property
    def url(self) -> str:
        if self.kind == "pages":
            return f"/{self.slug}/"
        if self.kind == "til":
            return f"/til/{self.topic}/{self.slug}/"
        return f"/{self.kind}/{self.slug}/"


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFC", str(name)).strip()
    name = re.sub(r"[\s]+", "-", name)
    name = re.sub(r'[?#%/\\"<>|:*\[\]{}^`]', "", name)
    return name


FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def parse_date(value, fallback: Path) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    return dt.date.fromtimestamp(fallback.stat().st_mtime)


def load_note(path: Path, kind: str) -> Note:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    m = FRONT_MATTER.match(text)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        text = text[m.end():]
    title = path.stem
    lines = text.lstrip("\n").split("\n")
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        text = "\n".join(lines[1:])
    topic = path.parent.name if kind == "til" and path.parent.name != "til" else None
    if kind == "til" and topic is None:
        topic = str(meta.get("topic") or "misc")
    return Note(
        path=path,
        kind=kind,
        stem=path.stem,
        meta=meta,
        title=title,
        body=text,
        date=parse_date(meta.get("date"), path),
        topic=topic,
    )


def load_all() -> list[Note]:
    notes = []
    for kind in ["til", "tif", "writeups", "pages"]:
        base = CONTENT / kind
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            notes.append(load_note(path, kind))
    return notes


# ---------------------------------------------------------------- math

FRAKTUR_EXC = {"C": 0x212D, "H": 0x210C, "I": 0x2111, "R": 0x211C, "Z": 0x2128}
SCRIPT_EXC = {"B": 0x212C, "E": 0x2130, "F": 0x2131, "H": 0x210B, "I": 0x2110, "L": 0x2112,
              "M": 0x2133, "R": 0x211B, "e": 0x212F, "g": 0x210A, "o": 0x2134}
DOUBLE_EXC = {"C": 0x2102, "H": 0x210D, "N": 0x2115, "P": 0x2119, "Q": 0x211A, "R": 0x211D, "Z": 0x2124}
VARIANTS = {  # mathvariant -> (capital base, small base, exceptions)
    "fraktur": (0x1D504, 0x1D51E, FRAKTUR_EXC),
    "script": (0x1D49C, 0x1D4B6, SCRIPT_EXC),
    "double-struck": (0x1D538, 0x1D552, DOUBLE_EXC),
    "bold": (0x1D400, 0x1D41A, {}),
}


def to_variant(text: str, variant: str) -> str | None:
    if variant not in VARIANTS:
        return None
    cap, small, exc = VARIANTS[variant]
    out = []
    for ch in text:
        if ch in exc:
            out.append(chr(exc[ch]))
        elif "A" <= ch <= "Z":
            out.append(chr(cap + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(small + ord(ch) - ord("a")))
        else:
            return None
    return "".join(out)


MI_VARIANT = re.compile(r'<mi mathvariant="([a-z-]+)">([^<]*)</mi>')
MI_PLAIN = re.compile(r"<mi>([^<]{1})</mi>")


def polish_mathml(s: str) -> str:
    """Adapt KaTeX MathML to MathML Core + the upright Euler look."""
    s = re.sub(r'^<span class="katex(?:-display)?">', "", s)
    s = re.sub(r"</span>$", "", s)
    s = re.sub(r"<annotation encoding=\"application/x-tex\">.*?</annotation>", "", s, flags=re.S)
    s = s.replace("<semantics>", "").replace("</semantics>", "")

    def variant(m):
        conv = to_variant(m.group(2), m.group(1))
        if conv is None:
            return m.group(0)
        return f'<mi mathvariant="normal">{conv}</mi>'

    s = MI_VARIANT.sub(variant, s)
    # Concrete Mathematics sets Euler letters upright: stop automatic italic on single letters.
    s = MI_PLAIN.sub(r'<mi mathvariant="normal">\1</mi>', s)
    # set-builder colon: relation-like spacing
    s = s.replace("<mo>:</mo>", '<mo lspace="0.2778em" rspace="0.2778em">:</mo>')
    return s


def render_math(items: list[dict]) -> list[str]:
    if not items:
        return []
    proc = subprocess.run(
        ["node", str(ROOT / "katex_render.mjs")],
        input=json.dumps(items),
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    return [polish_mathml(x) for x in json.loads(proc.stdout)]


# ---------------------------------------------------------------- markdown

WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")


def make_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "typographer": False})
    md.enable("table").enable("strikethrough")
    md.use(front_matter_plugin)
    md.use(footnote_plugin)
    md.use(tasklists_plugin)
    md.use(dollarmath_plugin, allow_space=False, double_inline=True)
    return md


def collect_math(md: MarkdownIt, text: str) -> list[dict]:
    items = []

    def walk(tokens):
        for t in tokens:
            if t.type in ("math_inline",):
                items.append({"tex": t.content, "display": False})
            elif t.type in ("math_inline_double", "math_block", "math_block_label"):
                items.append({"tex": t.content, "display": True})
            if t.children:
                walk(t.children)

    walk(md.parse(text))
    return items


def render_note_body(md: MarkdownIt, note: Note, by_stem: dict[str, Note], math_iter) -> str:
    def link(m):
        target, label = m.group(1).strip(), (m.group(2) or m.group(1)).strip()
        other = by_stem.get(target)
        if other is None:
            return f'<span class="wikilink-missing">{html.escape(label)}</span>'
        if note not in other.backlinks:
            other.backlinks.append(note)
        return f'<a class="wikilink" href="{other.url}">{html.escape(label)}</a>'

    text = WIKILINK.sub(link, note.body)

    renderer = md.renderer
    renderer.rules["math_inline"] = lambda *a, **k: f'<span class="math-inline">{next(math_iter)}</span>'
    display = lambda *a, **k: f'<div class="math-display">{next(math_iter)}</div>\n'
    renderer.rules["math_inline_double"] = display
    renderer.rules["math_block"] = display
    renderer.rules["math_block_label"] = display
    return md.render(text)


# ---------------------------------------------------------------- pages

NAV = [("/til/", "TIL"), ("/tif/", "TIF"), ("/writeups/", "結案"), ("/about/", "關於")]


def page(title: str, body: str, *, head_extra: str = "") -> str:
    nav = " ".join(f'<a href="{u}">{t}</a>' for u, t in NAV)
    full_title = SITE_TITLE if title == SITE_TITLE else f"{title} · {SITE_TITLE}"
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(full_title)}</title>
<link rel="stylesheet" href="/static/fonts/iansui/iansui.css">
<link rel="stylesheet" href="/static/style.css?v={CSS_VERSION}">
<link rel="alternate" type="application/atom+xml" title="{SITE_TITLE}" href="/feed.xml">
{head_extra}</head>
<body>
<header class="site"><a class="brand" href="/">{SITE_TITLE}</a><nav>{nav}</nav></header>
<main>
{body}
</main>
<footer class="site"><a href="/feed.xml">RSS</a></footer>
</body>
</html>
"""


def kind_label(n: Note) -> str:
    if n.kind == "til":
        return f'<a class="kind" href="/til/{n.topic}/">TIL · {html.escape(n.topic)}</a>'
    if n.kind in SECTIONS:
        return f'<a class="kind" href="/{n.kind}/">{SECTIONS[n.kind]}</a>'
    return ""


def entry_list(notes: list[Note], show_kind: bool = True) -> str:
    rows = []
    for n in notes:
        kind = f" {kind_label(n)}" if show_kind else ""
        rows.append(
            f'<li><time datetime="{n.date.isoformat()}">{n.date.isoformat()}</time>'
            f'<a class="entry" href="{n.url}">{html.escape(n.title)}</a>{kind}</li>'
        )
    return '<ul class="entries">\n' + "\n".join(rows) + "\n</ul>"


def note_page(n: Note) -> str:
    meta_bits = []
    if n.kind != "pages":
        meta_bits.append(f'<time datetime="{n.date.isoformat()}">{n.date.isoformat()}</time>')
        meta_bits.append(kind_label(n))
    source = n.meta.get("source")
    if source:
        s = str(source)
        if s.startswith("http"):
            meta_bits.append(f'<a href="{html.escape(s)}">來源</a>')
        else:
            meta_bits.append(f"來源：{html.escape(s)}")
    meta = f'<p class="meta">{" · ".join(meta_bits)}</p>' if meta_bits else ""
    back = ""
    if n.backlinks:
        items = "".join(f'<li><a href="{b.url}">{html.escape(b.title)}</a></li>' for b in n.backlinks)
        back = f'<aside class="backlinks"><h2>連到這裡的文章</h2><ul>{items}</ul></aside>'
    body = f'<article><h1>{html.escape(n.title)}</h1>{meta}\n{n.html}</article>\n{back}'
    return page(n.title, body)


def atom(notes: list[Note], title: str, path: str) -> str:
    updated = max((n.date for n in notes), default=dt.date.today()).isoformat()
    entries = []
    for n in notes[:30]:
        entries.append(f"""<entry>
<title>{html.escape(n.title)}</title>
<link href="{SITE_URL}{n.url}"/>
<id>{SITE_URL}{n.url}</id>
<updated>{n.date.isoformat()}T00:00:00+08:00</updated>
<content type="html">{html.escape(n.html)}</content>
</entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>{html.escape(title)}</title>
<link href="{SITE_URL}{path}" rel="self"/>
<link href="{SITE_URL}/"/>
<id>{SITE_URL}{path}</id>
<updated>{updated}T00:00:00+08:00</updated>
{chr(10).join(entries)}
</feed>
"""


def write(rel: str, text: str) -> None:
    target = OUT / rel.lstrip("/")
    if rel.endswith("/"):
        target = target / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    notes = load_all()
    by_stem = {n.stem: n for n in notes}
    md = make_md()

    # one KaTeX call for every formula on the site
    per_note = [collect_math(md, WIKILINK.sub(lambda m: m.group(0), n.body)) for n in notes]
    flat = render_math([item for items in per_note for item in items])
    pos = 0
    for n, items in zip(notes, per_note):
        rendered = iter(flat[pos:pos + len(items)])
        pos += len(items)
        n.html = render_note_body(md, n, by_stem, rendered)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copytree(STATIC, OUT / "static")
    (OUT / ".nojekyll").write_text("")

    posts = sorted((n for n in notes if n.kind != "pages"), key=lambda n: (n.date, n.title), reverse=True)

    for n in notes:
        write(n.url, note_page(n))

    # home
    intro = '<p class="lead">讀書時學到的（TIL）、找到的（TIF），以及讀完一本書後的結案文章。<a href="/about/">關於這個網站</a>。</p>'
    listing = entry_list(posts[:40]) if posts else '<p class="empty">還沒有文章。</p>'
    write("/", page(SITE_TITLE, intro + listing))
    write("/feed.xml", atom(posts, SITE_TITLE, "/feed.xml"))

    # sections
    for kind, label in SECTIONS.items():
        items = [n for n in posts if n.kind == kind]
        if kind == "til":
            topics: dict[str, list[Note]] = {}
            for n in items:
                topics.setdefault(n.topic, []).append(n)
            parts = []
            for topic in sorted(topics):
                parts.append(f'<h2><a href="/til/{topic}/">{html.escape(topic)}</a></h2>' + entry_list(topics[topic], False))
                write(f"/til/{topic}/", page(f"TIL · {topic}", f"<h1>TIL · {html.escape(topic)}</h1>" + entry_list(topics[topic], False)))
                write(f"/til/{topic}/feed.xml", atom(topics[topic], f"{SITE_TITLE} TIL · {topic}", f"/til/{topic}/feed.xml"))
            body = "<h1>TIL</h1>" + ("".join(parts) or '<p class="empty">還沒有 TIL。</p>')
        else:
            body = f"<h1>{label}</h1>" + (entry_list(items, False) if items else f'<p class="empty">還沒有{label}。</p>')
        write(f"/{kind}/", page(label, body))
        write(f"/{kind}/feed.xml", atom(items, f"{SITE_TITLE} {label}", f"/{kind}/feed.xml"))

    print(f"built {len(notes)} notes into {OUT}")


if __name__ == "__main__":
    main()
