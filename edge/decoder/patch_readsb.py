#!/usr/bin/env python3
"""Patch the pinned readsb net_io.c build with opt-in JSONL per-message events."""

import shutil
import sys
from pathlib import Path


def patch(source_dir: Path) -> None:
    header = Path(__file__).with_name("readsb_event.h")
    if not header.is_file():
        raise SystemExit(f"readsb event header not found: {header}")
    path = source_dir / "net_io.c"
    if not path.is_file():
        raise SystemExit(f"readsb source file not found: {path}")
    text = path.read_text()

    include = '#include "readsb_event.h"'
    include_anchor = '#include "readsb.h"\n'
    if include in text:
        pass
    elif text.count(include_anchor) == 1:
        text = text.replace(include_anchor, include_anchor + include + "\n", 1)
    else:
        raise SystemExit("readsb net_io.c include anchor changed; refusing to patch")

    function_anchor = "static void outputMessage(struct modesMessage *mm) {\n"
    event_call = "    sky_emit(mm);\n"
    if event_call not in text:
        if text.count(function_anchor) != 1:
            raise SystemExit("readsb outputMessage anchor changed; refusing to patch")
        text = text.replace(function_anchor, function_anchor + event_call, 1)

    path.write_text(text)
    shutil.copyfile(header, source_dir / header.name)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} READSB_SOURCE_DIR")
    patch(Path(sys.argv[1]))
