"""Entry point used by ``briefcase run --test``.

Briefcase runs this module as ``__main__`` inside the app bundle (on iOS the
bundle is read-only and ``pyproject.toml`` isn't shipped, so everything pytest
needs is passed on the command line here). Briefcase watches the log for the
``EXIT`` marker to learn the result.
"""

import os
import sys
from pathlib import Path

import pytest


def run_tests() -> int:
    project_path = Path(__file__).parent.parent
    os.chdir(project_path)
    returncode = pytest.main(
        [
            "-vv",
            "--color=no",
            "-p",
            "no:cacheprovider",  # the app bundle is read-only
            "-o",
            "asyncio_mode=auto",
            f"--rootdir={project_path}",
            str(project_path / "tests"),
        ]
    )
    print(f">>>>>>>>>> EXIT {returncode} <<<<<<<<<<")
    return int(returncode)


if __name__ == "__main__":
    sys.exit(run_tests())
