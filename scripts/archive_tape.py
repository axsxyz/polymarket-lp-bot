#!/usr/bin/env python3
"""Archive data/tape.jsonl with a local timestamp in the filename.

Usage:
    python scripts/archive_tape.py
        Move data/tape.jsonl → data/archives/tape_YYYYMMDD_HHMMSS.jsonl

    python scripts/archive_tape.py --watch
        Watch data/archives: when a bare tape.jsonl appears (drag-drop),
        rename it to tape_YYYYMMDD_HHMMSS.jsonl.

Prefer stopping the bot (or waiting between writes) before moving the live tape.
LocalTape opens append-per-write, so a fresh data/tape.jsonl is recreated on
the next event after a move.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "data" / "tape.jsonl"
ARCHIVES = ROOT / "data" / "archives"
BARE_NAME = "tape.jsonl"


def stamped_name(when: datetime | None = None) -> str:
    ts = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"tape_{ts}.jsonl"


def unique_dest(folder: Path, when: datetime | None = None) -> Path:
    """Pick tape_YYYYMMDD_HHMMSS.jsonl; bump with _2, _3… if collision."""
    base = stamped_name(when)
    dest = folder / base
    if not dest.exists():
        return dest
    stem = base.removesuffix(".jsonl")
    n = 2
    while True:
        candidate = folder / f"{stem}_{n}.jsonl"
        if not candidate.exists():
            return candidate
        n += 1


def wait_stable(path: Path, *, checks: int = 3, interval: float = 0.15) -> bool:
    """Wait until size stops changing (Explorer move may still be writing)."""
    last = -1
    stable = 0
    for _ in range(40):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
            last = size
        time.sleep(interval)
    return path.exists()


def rename_bare_in_archives(path: Path) -> Path | None:
    """If path is archives/tape.jsonl, rename with local stamp. Else None."""
    if path.name != BARE_NAME or path.parent.resolve() != ARCHIVES.resolve():
        return None
    if not path.is_file():
        return None
    if not wait_stable(path):
        print(f"skip (unstable or gone): {path}", file=sys.stderr)
        return None
    dest = unique_dest(ARCHIVES)
    path.rename(dest)
    print(f"renamed {path.name} -> {dest.name}")
    return dest


def archive_live_tape() -> Path:
    """Move data/tape.jsonl into archives with a timestamped name."""
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    if not TAPE.is_file():
        raise FileNotFoundError(f"no live tape at {TAPE}")
    dest = unique_dest(ARCHIVES)
    TAPE.rename(dest)
    print(f"archived {TAPE.relative_to(ROOT)} -> {dest.relative_to(ROOT)}")
    return dest


def watch_archives() -> None:
    """Rename any bare tape.jsonl dropped into data/archives."""
    try:
        from watchfiles import Change, watch
    except ImportError as exc:
        raise SystemExit(
            "watchfiles required for --watch (already in requirements.txt)"
        ) from exc

    ARCHIVES.mkdir(parents=True, exist_ok=True)
    print(f"watching {ARCHIVES.relative_to(ROOT)} for {BARE_NAME} (Ctrl+C to stop)")

    # Catch a file already sitting there when the watcher starts.
    existing = ARCHIVES / BARE_NAME
    if existing.is_file():
        rename_bare_in_archives(existing)

    for changes in watch(ARCHIVES, watch_filter=None):
        for change, raw in changes:
            if change not in (Change.added, Change.modified):
                continue
            path = Path(raw)
            if path.name == BARE_NAME:
                rename_bare_in_archives(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch",
        action="store_true",
        help="watch data/archives and auto-rename bare tape.jsonl drops",
    )
    args = parser.parse_args()
    try:
        if args.watch:
            watch_archives()
        else:
            archive_live_tape()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
