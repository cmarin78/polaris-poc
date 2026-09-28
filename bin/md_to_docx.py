#!/usr/bin/env python3
"""Convert RUN_REPORT.md to RUN_REPORT.docx preserving structure."""
from docx import Document
from docx.shared import Inches, Pt
from pathlib import Path
import re

SRC = Path("/home/cmarin78/Documents/Projects/MiniMax/Headscale/polaris/RUN_REPORT.md")
OUT = Path("/home/cmarin78/Documents/Projects/MiniMax/Headscale/polaris/RUN_REPORT.docx")


def add_inline(paragraph, text):
    """Parse inline markdown (bold, code, links) and add runs to a paragraph."""
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
        else:
            paragraph.add_run(part)


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
