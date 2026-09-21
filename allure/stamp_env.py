"""Write allure-results/environment.properties.

Loads .env the same way tests/conftest.py does — a bare `python -c` does not,
which is how an early version of this stamped every report with the default
base URL while the run had actually gone to staging.

Usage: python allure/stamp_env.py [results_dir]
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv() -> None:
    """Mirror tests/conftest.py: real environment variables win over the file."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_dotenv()
    import agentbox_sdk

    results = Path(sys.argv[1] if len(sys.argv) > 1 else "allure-results")
    results.mkdir(parents=True, exist_ok=True)

    base_url = os.getenv("GMI_AGENTBOX_BASE_URL", agentbox_sdk.DEFAULT_BASE_URL)
    environment = "production" if base_url == agentbox_sdk.DEFAULT_BASE_URL else "staging / dev"

    fields = {
        "Environment": environment,
        "Base.URL": base_url,
        "SDK.Version": agentbox_sdk.__version__,
        "Python.Version": platform.python_version(),
        "Platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "API.Key.Configured": "yes" if os.getenv("GMI_AGENTBOX_API_KEY") else "no",
    }

    (results / "environment.properties").write_text(
        "".join(f"{key}={value}\n" for key, value in fields.items())
    )

    categories = PROJECT_ROOT / "allure" / "categories.json"
    if categories.exists():
        (results / "categories.json").write_text(categories.read_text())

    for key, value in fields.items():
        print(f"  {key}={value}")


if __name__ == "__main__":
    main()
