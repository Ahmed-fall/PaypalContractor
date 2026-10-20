#!/usr/bin/env python3
"""Render shared/fixtures/contracts/*.md to canonical *.pdf via LibreOffice headless."""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "shared" / "fixtures" / "contracts"

CSS = """
body { font-family: "Liberation Serif", Georgia, serif; font-size: 11pt;
       line-height: 1.5; margin: 2.2cm; color: #111; }
h1 { font-size: 17pt; border-bottom: 2px solid #222; padding-bottom: 6px; }
h2 { font-size: 13pt; margin-top: 18px; }
p  { margin: 8px 0; }
li { margin: 4px 0; }
strong { font-weight: 700; }
"""


def md_to_html(md: str) -> str:
    out: list[str] = []
    list_open = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if list_open:
                out.append("</ul>")
                list_open = False
            continue
        text = html.escape(line)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        if line.startswith("## "):
            if list_open:
                out.append("</ul>")
                list_open = False
            out.append(f"<h2>{text[3:]}</h2>")
        elif line.startswith("# "):
            if list_open:
                out.append("</ul>")
                list_open = False
            out.append(f"<h1>{text[2:]}</h1>")
        elif line.startswith("- "):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{text[2:]}</li>")
        else:
            if list_open:
                out.append("</ul>")
                list_open = False
            out.append(f"<p>{text}</p>")
    if list_open:
        out.append("</ul>")
    body = "\n".join(out)
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"


def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("error: soffice/libreoffice not found on PATH")


def main() -> None:
    soffice = find_soffice()
    sources = sorted(CONTRACTS.glob("*.md"))
    if not sources:
        sys.exit(f"error: no *.md contracts found in {CONTRACTS}")
    with tempfile.TemporaryDirectory(prefix="fd-fixtures-") as tmp:
        tmpdir = Path(tmp)
        profile = tmpdir / "lo_profile"
        for src in sources:
            html_path = tmpdir / f"{src.stem}.html"
            html_path.write_text(md_to_html(src.read_text(encoding="utf-8")), encoding="utf-8")
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file://{profile}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(CONTRACTS),
                    str(html_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            produced = CONTRACTS / f"{src.stem}.pdf"
            if not produced.exists():
                sys.exit(f"error: expected {produced} was not produced")
            print(f"built {produced.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
