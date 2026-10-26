#!/usr/bin/env python3
"""Replace common OneDrive-based absolute paths with current absolute paths.

This script makes backups of files it edits (adds .bak) and prints a summary.
Run from repository root: `python scripts/fix_paths_replace_oneDrive.py`
"""
from pathlib import Path
import re
import sys
import os

ROOT = Path(__file__).resolve().parents[1]

# Ordered replacement rules (regex pattern -> replacement)
# Perform simple string replacements for common OneDrive path variants
# Build platform-correct path strings to avoid string-literal escaping issues
STRING_REPLS = []
def p(*parts):
    return "\\".join(parts)

STRING_REPLS.append((p('C:', 'Users', 'tetsu', 'OneDrive', 'デスクトップ', 'HFLs', 'Server_side'), p('C:', 'Users', 'tetsu', 'Serverside')))
STRING_REPLS.append((p('C:', 'Users', 'tetsu', 'OneDrive', 'Desktop', 'Serverside'), p('C:', 'Users', 'tetsu', 'Serverside')))
STRING_REPLS.append((p('C:', 'Users', 'tetsu', 'OneDrive', 'Desktop', 'HFLs', 'Server_side'), p('C:', 'Users', 'tetsu', 'Serverside')))
STRING_REPLS.append((p('C:', 'Users', 'tetsu', 'OneDrive'), p('C:', 'Users', 'tetsu')))

TEXT_EXT = {'.py', '.txt', '.md', '.json', '.log', '.ps1', '.cfg', '.ini', '.csv', '.jsonl', '.yaml', '.yml', '.rst'}


def should_process(p: Path) -> bool:
    if p.is_dir():
        return False
    if p.suffix.lower() in TEXT_EXT:
        return True
    # try small files as text
    try:
        if p.stat().st_size > 5_000_000:
            return False
    except Exception:
        return False
    return True


def process_file(p: Path):
    try:
        s = p.read_text(encoding='utf-8')
    except Exception:
        try:
            s = p.read_text(encoding='cp932')
        except Exception:
            return False

    new = s
    # Apply replacements case-insensitively, handle escaped (double-backslash) variants too
    for old, newval in STRING_REPLS:
        try:
            pat = re.compile(re.escape(old), flags=re.IGNORECASE)
            new = pat.sub(newval, new)
        except Exception:
            new = new.replace(old, newval)
        dbl_old = old.replace('\\', '\\\\')
        dbl_new = newval.replace('\\', '\\\\')
        try:
            pat2 = re.compile(re.escape(dbl_old), flags=re.IGNORECASE)
            new = pat2.sub(dbl_new, new)
        except Exception:
            new = new.replace(dbl_old, dbl_new)

    if new != s:
        bak = p.with_suffix(p.suffix + '.bak')
        try:
            p.rename(bak)
            bak.write_text(new, encoding='utf-8')
            # restore original filename: move bak -> original after writing new content
            bak.replace(p)
        except Exception:
            # fallback: write .bak and overwrite original
            try:
                p.write_text(new, encoding='utf-8')
            except Exception as e:
                print(f"Failed to write {p}: {e}")
                return False
        print(f"Updated: {p}")
        return True
    return False


def main():
    changed = 0
    scanned = 0
    # Diagnostic: count occurrences of each search string across repo
    for old, newval in STRING_REPLS:
        total = 0
        for p in ROOT.rglob('*'):
            if not should_process(p):
                continue
            try:
                txt = p.read_text(encoding='utf-8')
            except Exception:
                try:
                    txt = p.read_text(encoding='cp932')
                except Exception:
                    continue
            total += txt.count(old)
        print(f"Diagnostic: occurrences of '{old}': {total}")
    # Find and print a sample occurrence of 'OneDrive' for inspection
    sample_printed = False
    for p in ROOT.rglob('*'):
        if sample_printed:
            break
        if not should_process(p):
            continue
        try:
            txt = p.read_text(encoding='utf-8')
        except Exception:
            try:
                txt = p.read_text(encoding='cp932')
            except Exception:
                continue
        idx = txt.find('OneDrive')
        if idx != -1:
            start = max(0, idx - 40)
            end = min(len(txt), idx + 120)
            snippet = txt[start:end]
            print('Sample file:', p)
            print('Snippet repr:', repr(snippet))
            sample_printed = True
    for p in ROOT.rglob('*'):
        if not should_process(p):
            continue
        scanned += 1
        try:
            if process_file(p):
                changed += 1
        except Exception as e:
            print(f"Error processing {p}: {e}")

    print(f"Scanned files: {scanned}")
    print(f"Files changed: {changed}")
    if changed == 0:
        print("No replacements made.")


if __name__ == '__main__':
    main()
