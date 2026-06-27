#!/usr/bin/env python3
"""Documentation checks for ws63-qemu (run in CI by .github/workflows/docs.yml).

Deterministic and offline — validates the *living* doc set (docs/** plus the
top-level README/ROADMAP and patches/README). External URLs are NOT checked here
(that is the link-check.yml workflow's job, via lychee). CHANGELOG.md is excluded
on purpose: it is a historical record whose links describe past releases.

Checks:
  1. Internal links resolve   — every [text](relative/path) points at a real file.
  2. Anchors resolve          — [..](file.md#anchor) matches a heading slug.
  3. Diataxis layout          — the four category dirs exist and are non-empty.
  4. No orphan pages          — every docs/**/*.md is linked from docs/README.md.
  5. Every page has an H1     — first heading line is a level-1 title.
  6. No stale paths           — removed pre-reorg filenames are not referenced.

Exit status is non-zero if any check fails.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# The maintained ("living") markdown set. CHANGELOG.md and qemu/ are excluded.
EXTRA = ["README.md", "ROADMAP.md", "patches/README.md"]
DOC_FILES = sorted(
    {*(str(p) for p in Path("docs").rglob("*.md")), *EXTRA}
)

# Filenames that existed before the Diataxis reorg and must no longer be linked
# from the living docs (they were split/moved into the four categories).
STALE = [
    "docs/user-manual.md", "docs/design.md", "docs/memory-map.md",
    "docs/rom-stubs.md", "docs/xlinx-isa.md", "docs/rust-toolchain-xlinx.md",
    "docs/alignment-analysis.md", "docs/bs21-connectivity-feasibility.md",
    "docs/bs21-vendor-firmware.md",
]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def slug(heading: str) -> str:
    """Approximate GitHub's heading-to-anchor algorithm (GFM, Unicode-aware)."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)  # drop punctuation, keep CJK
    s = s.replace(" ", "-")
    return s


def headings_of(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING_RE.match(line)
        if m and not line.lstrip().startswith("```"):
            out.add(slug(m.group(2)))
    return out


def first_heading_level(path: Path) -> int | None:
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            return len(m.group(1))
    return None


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "tel:"))


# --- 1 & 2: internal links + anchors -----------------------------------------
heading_cache: dict[str, set[str]] = {}
for f in DOC_FILES:
    fp = Path(f)
    if not fp.exists():
        err(f"listed doc file missing: {f}")
        continue
    text = fp.read_text(encoding="utf-8")
    for m in LINK_RE.finditer(text):
        target = m.group(1).strip()
        if not target or is_external(target) or target.startswith("#"):
            # in-file anchor
            if target.startswith("#"):
                anchor = target[1:]
                if anchor and anchor not in heading_cache.setdefault(f, headings_of(fp)):
                    err(f"{f}: in-file anchor not found: {target}")
            continue
        path_part, _, anchor = target.partition("#")
        resolved = (fp.parent / path_part).resolve()
        if not resolved.exists():
            err(f"{f}: broken link -> {target}")
            continue
        if anchor and resolved.suffix == ".md":
            key = str(resolved.relative_to(ROOT))
            slugs = heading_cache.setdefault(key, headings_of(resolved))
            if anchor not in slugs:
                err(f"{f}: anchor not found -> {target}")

# --- 3: Diataxis layout -------------------------------------------------------
for cat in ("tutorials", "how-to", "reference", "explanation"):
    d = Path("docs") / cat
    if not d.is_dir() or not list(d.glob("*.md")):
        err(f"Diataxis category missing or empty: docs/{cat}/")

# --- 4: orphan pages (every page is in the mdBook table of contents) ----------
# docs/SUMMARY.md is the authoritative TOC: a page not listed there is dropped
# from the built site (mdBook itself warns about it), so require every page to
# appear in SUMMARY.md. SUMMARY.md itself is the TOC, not a content page.
toc = Path("docs/SUMMARY.md")
if toc.exists():
    linked: set[str] = set()
    for m in LINK_RE.finditer(toc.read_text(encoding="utf-8")):
        t = m.group(1).split("#")[0].strip()
        if not t or is_external(t):
            continue
        r = (toc.parent / t).resolve()
        if r.exists() and r.suffix == ".md":
            linked.add(str(r.relative_to(ROOT)))
    for p in Path("docs").rglob("*.md"):
        rel = str(p)
        if rel == "docs/SUMMARY.md":
            continue
        if rel not in linked:
            err(f"page missing from docs/SUMMARY.md (mdBook TOC): {rel}")
else:
    err("docs/SUMMARY.md (mdBook table of contents) is missing")

# --- 5: every page has an H1 --------------------------------------------------
for p in Path("docs").rglob("*.md"):
    lvl = first_heading_level(p)
    if lvl is None:
        err(f"{p}: no heading found (expected an H1 title)")
    elif lvl != 1:
        err(f"{p}: first heading is H{lvl}, expected H1")

# --- 6: no stale pre-reorg paths ----------------------------------------------
for f in DOC_FILES:
    text = Path(f).read_text(encoding="utf-8")
    for stale in STALE:
        if f"]({stale})" in text or f"]({stale}#" in text:
            err(f"{f}: references removed doc path: {stale}")

# --- report -------------------------------------------------------------------
if errors:
    print(f"docs check: {len(errors)} problem(s)\n", file=sys.stderr)
    for e in errors:
        print(f"  ✗ {e}", file=sys.stderr)
    sys.exit(1)

print(f"docs check: OK ({len(DOC_FILES)} files, links + anchors + layout + content)")
