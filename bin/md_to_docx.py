#!/usr/bin/env python3
"""Convert RUN_REPORT.md to RUN_REPORT.docx preserving structure.

Embeds images referenced via markdown image syntax
(![alt](path/to/file.png)) as inline figures with the alt text as
caption. Resolves paths relative to RUN_REPORT.md so something like
`![Foo](docs/screenshots/01.png)` lands correctly when the script is
run from any cwd.
"""
from docx import Document
from docx.shared import Inches, Pt
from pathlib import Path
import re

ROOT = Path("/home/cmarin78/Documents/Projects/MiniMax/Headscale/polaris")
SRC = ROOT / "RUN_REPORT.md"
OUT = ROOT / "RUN_REPORT.docx"

# Matches ![alt text](path/to/image.png) — alt may be empty.
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def add_inline(paragraph, text):
    """Parse inline markdown (bold, code, links) and add runs to a paragraph.
    Strips out image syntax — those are handled separately at the
    paragraph level so we can add the image AND the alt-text caption.
    """
    # very small subset: just split on ** and `
    parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(10)
        elif IMG_RE.match(part):
            # Should not happen because we strip images at the paragraph
            # level, but in case an image shows up mid-line we skip it.
            continue
        else:
            paragraph.add_run(part)


def add_image_with_caption(doc, alt, path):
    """Add an inline image, centered, with the alt text as italic caption."""
    img_path = (ROOT / path).resolve()
    if not img_path.exists():
        # Surface the missing file so the doc doesn't silently lose it.
        p = doc.add_paragraph()
        run = p.add_run(f"[missing image: {path}]")
        run.italic = True
        run.font.color.rgb = None  # default
        return

    p = doc.add_paragraph()
    p.alignment = 1  # WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    # Cap image width at 6.0 inches so it fits A4 with margins.
    run.add_picture(str(img_path), width=Inches(6.0))

    if alt:
        cap = doc.add_paragraph()
        cap.alignment = 1
        cap_run = cap.add_run(alt)
        cap_run.italic = True
        cap_run.font.size = Pt(10)


def main():
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    in_code = False
    code_lines = []

    for raw in SRC.read_text().splitlines():
        line = raw.rstrip()

        # code fences
        if line.startswith("```"):
            if in_code:
                p = doc.add_paragraph()
                p.style = doc.styles["No Spacing"]
                run = p.add_run("\n".join(code_lines))
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            doc.add_heading(m.group(2), level=level)
            continue

        # table
        if line.startswith("|") and line.endswith("|"):
            # crude table detection
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                continue  # separator row
            row = doc.add_table(rows=1, cols=len(cells))
            for i, c in enumerate(cells):
                cell = row.rows[0].cells[i]
                cell.text = ""
                p = cell.paragraphs[0]
                add_inline(p, c)
            continue

        # bullet
        m = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if m:
            indent = len(m.group(1))
            p = doc.add_paragraph(style="List Bullet")
            add_inline(p, m.group(2))
            if indent:
                p.paragraph_format.left_indent = Inches(0.5 * (indent // 2 + 1))
            continue

        # numbered
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            p = doc.add_paragraph(style="List Number")
            add_inline(p, m.group(1))
            continue

        # image — must be its own line (markdown convention)
        m = IMG_RE.match(line)
        if m:
            add_image_with_caption(doc, m.group(1).strip(), m.group(2).strip())
            continue

        # blank
        if not line:
            doc.add_paragraph()
            continue

        # default paragraph
        p = doc.add_paragraph()
        add_inline(p, line)

    doc.save(OUT)
    print(f"saved {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
