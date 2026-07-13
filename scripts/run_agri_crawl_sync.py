#!/usr/bin/env python3
"""智慧农业爬取同步入口（等同 run_daily_agri_sync，供 script-cron / Hermes 调用）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.run_daily_agri_sync import main

if __name__ == "__main__":
    asyncio.run(main())
