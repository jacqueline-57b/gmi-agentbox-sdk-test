"""Load PROJECT_ROOT/.env without pulling in python-dotenv.

It lives here rather than in conftest.py because two callers need it now: the
suite, through conftest, and tests/test_b7_quickstart/quickstart.py, which is
run on its own and would otherwise keep a second copy of the same parser.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(env_file: Optional[Path] = None) -> None:
    """Read `.env` into os.environ. Existing variables win.

    `GMI_AGENTBOX_API_KEY=... pytest` still overrides the file.
    """
    env_file = env_file or PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
