"""
PDF to Markdown converter using pdfminer.six
Usage:  python pdf2md.py "input.pdf" "output.md"
        python pdf2md.py "input.pdf"          (output = input stem + .md)
"""
import sys
from pathlib import Path
from pdfminer.high_level import extract_text

def convert(pdf_path: str, md_path: str = None):
    pdf = Path(pdf_path)
    if not pdf.exists():
        print(f"Error: file not found — {pdf}"); return

    if md_path is None:
        md_path = str(pdf.with_suffix(".md"))

    print(f"Reading {pdf} …")
    text = extract_text(str(pdf))

    # Basic formatting: blank lines → paragraph breaks
    lines = text.splitlines()
    md_lines = []
    for line in lines:
        stripped = line.strip()
        md_lines.append(stripped)

    md = "\n\n".join(l for l in md_lines if l)

    Path(md_path).write_text(md, encoding="utf-8")
    print(f"✓  Saved → {md_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pdf2md.py input.pdf [output.md]")
        sys.exit(1)
    pdf_in  = sys.argv[1]
    md_out  = sys.argv[2] if len(sys.argv) > 2 else None
    convert(pdf_in, md_out)
