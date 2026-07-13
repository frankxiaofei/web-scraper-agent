#!/usr/bin/env python3
"""BIM 招标公告全量/增量打标入口（复用 backfill_bim_classify）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backfill_bim_classify import main

if __name__ == "__main__":
    raise SystemExit(main())
