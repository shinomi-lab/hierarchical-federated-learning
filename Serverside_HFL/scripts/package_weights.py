#!/usr/bin/env python3
"""
Copy a model weight file into an Android app's assets (or res/raw) directory,
compute SHA256 and size, and write a small metadata JSON next to it.

Usage:
  python scripts/package_weights.py --source path/to/weights.pt

By default copies to `app/src/main/assets/weights/` under workspace root.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys


def sha256_hex(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def package_weights(src: Path, dest_dir: Path, dest_name: str | None = None, write_meta: bool = True) -> dict:
    if not src.exists():
        raise FileNotFoundError(f"source not found: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_name:
        dest = dest_dir / dest_name
    else:
        dest = dest_dir / src.name
    shutil.copy2(src, dest)
    sha = sha256_hex(dest)
    size = dest.stat().st_size
    meta = {
        "filename": dest.name,
        "rel_path": str(dest.relative_to(Path.cwd())),
        "sha256": sha,
        "size_bytes": size,
    }
    if write_meta:
        meta_path = dest.with_suffix(dest.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Package model weights into Android app assets")
    p.add_argument("--source", "-s", required=True, help="Path to source weights file")
    p.add_argument("--dest", "-d", default="app/src/main/assets/weights/", help="Destination directory inside repo (default: app/src/main/assets/weights/)")
    p.add_argument("--name", "-n", default=None, help="Optional destination filename")
    p.add_argument("--no-meta", dest="meta", action="store_false", help="Do not write meta json next to weights")
    args = p.parse_args(argv)

    src = Path(args.source).expanduser().resolve()
    dest_dir = Path(args.dest).expanduser()
    # If user provided a relative dest, resolve relative to repo root (cwd)
    if not dest_dir.is_absolute():
        dest_dir = (Path.cwd() / dest_dir).resolve()

    try:
        meta = package_weights(src, dest_dir, args.name, args.meta)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print("Packaged weights:")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print()
    print("Next steps:")
    print(f" - Verify the file is included in your Android project at: {dest_dir}")
    print(" - In Kotlin, load from assets via AssetManager or place into res/raw and use resources.openRawResource")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
