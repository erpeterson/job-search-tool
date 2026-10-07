"""Release and schema version attribution.

``main`` before versioning began is implied v0 (schema ``user_version`` 0). Bump
``__version__`` on every branch that changes behavior or schema, and
``SCHEMA_VERSION`` whenever ``Database.create_schema`` changes the stored shape.
"""

import subprocess
from pathlib import Path

__version__ = "1"
SCHEMA_VERSION = 1


def git_sha(repo_dir):
    """Short commit of the checkout, or ``unknown`` outside git or without the binary."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


def build_id(repo_dir=None):
    """``<version>+<sha>`` identifying exactly which code is running."""
    return f"{__version__}+{git_sha(repo_dir or Path(__file__).resolve().parent.parent)}"
