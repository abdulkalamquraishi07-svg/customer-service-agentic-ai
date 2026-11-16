"""Render the Markdown reports in docs/reports/ to PDF (headless Chromium via Playwright).

    pip install markdown==3.7 playwright && python -m playwright install chromium
    python scripts/build_pdfs.py

```mermaid blocks become diagrams (Mermaid is loaded from a CDN at build time, so the build
needs internet access). Output: docs/reports/pdf/<name>.pdf.
"""

import html
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "reports"
OUT = SRC / "pdf"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; color: #1e293b;
       line-height: 1.45; }
h1 { font-size: 20pt; color: #0f172a; border-bottom: 3px solid #f59e0b; padding-bottom: 4px;
     margin-top: 0; page-break-before: always; }
h1.first, .cover + h1 { page-break-before: avoid; }
h2 { font-size: 14pt; color: #0f172a; margin-top: 18px; border-bottom: 1px solid #cbd5e1; }
h3 { font-size: 11.5pt; color: #334155; margin-top: 14px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 9pt;
        page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #0f172a; color: #fff; text-align: left; padding: 5px 6px; }
td { border: 1px solid #cbd5e1; padding: 4px 6px; vertical-align: top; }
tr:nth-child(even) td { background: #f8fafc; }
code { font-family: Consolas, monospace; font-size: 8.8pt; background: #f1f5f9;
       padding: 0 3px; border-radius: 3px; }
pre { background: #f1f5f9; padding: 8px; border-radius: 4px; white-space: pre-wrap;
      font-size: 8.5pt; }
pre.mermaid { background: #fff; text-align: center; page-break-inside: avoid; }
pre.mermaid svg { max-width: 100% !important; max-height: 215mm; height: auto; }
blockquote { border-left: 4px solid #f59e0b; background: #fffbeb; margin: 10px 0;
             padding: 6px 10px; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .kicker { color: #b45309; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
.cover .title { font-size: 28pt; font-weight: 700; color: #0f172a; margin: 8px 0; }
.cover .subtitle { font-size: 14pt; color: #475569; }
.cover .meta { margin-top: 40px; font-size: 10pt; color: #475569; }
.placeholder { background: #fef3c7; padding: 0 3px; }
"""


def to_html(md_text: str, title: str) -> str:
    # Pull out the cover block (between <!--cover--> markers) untouched.
    cover = ""
    m = re.search(r"<!--cover-->(.*?)<!--/cover-->", md_text, re.S)
    if m:
        cover = m.group(1)
        md_text = md_text.replace(m.group(0), "")

    diagrams: list[str] = []

    def stash(match: re.Match[str]) -> str:
        diagrams.append(match.group(1))
        return f"\n\nMERMAIDBLOCK{len(diagrams) - 1}\n\n"

    md_text = re.sub(r"```mermaid\n(.*?)```", stash, md_text, flags=re.S)
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    for i, src in enumerate(diagrams):
        body = body.replace(
            f"<p>MERMAIDBLOCK{i}</p>", f'<pre class="mermaid">{html.escape(src)}</pre>'
        )
    # [[placeholder]] -> highlighted text the company must fill in.
    body = re.sub(r"\[\[(.+?)\]\]", r'<span class="placeholder">[\1]</span>', body)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{CSS}</style><script src="{MERMAID}"></script></head>
<body><div class="cover">{cover}</div>{body}
<script>
mermaid.initialize({{ startOnLoad: false, theme: "neutral", flowchart: {{ htmlLabels: true }} }});
mermaid.run().then(() => {{ document.body.dataset.ready = "1"; }})
  .catch(e => {{ document.body.dataset.ready = "error:" + e; }});
</script></body></html>"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = sorted(SRC.glob("*.md"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for src in sources:
            title = src.stem.replace("-", " ").title()
            page_html = to_html(src.read_text(encoding="utf-8"), title)
            html_path = OUT / f"{src.stem}.html"
            html_path.write_text(page_html, encoding="utf-8")
            page = browser.new_page()
            page.goto(html_path.as_uri())
            page.wait_for_function("document.body.dataset.ready !== undefined", timeout=60_000)
            state = page.evaluate("document.body.dataset.ready")
            if state != "1":
                print(f"{src.name}: diagram rendering failed: {state}", file=sys.stderr)
                return 1
            pdf = OUT / f"{src.stem}.pdf"
            page.pdf(
                path=str(pdf),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=(
                    '<div style="font-size:8px;width:100%;text-align:center;color:#64748b">'
                    f"{html.escape(title)} - page <span class='pageNumber'></span> of "
                    "<span class='totalPages'></span></div>"
                ),
                margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
            )
            page.close()
            html_path.unlink()
            print(f"wrote {pdf.relative_to(ROOT)}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
