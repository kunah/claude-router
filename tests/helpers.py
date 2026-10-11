"""Small cross-platform helpers for the tests."""

from __future__ import annotations

import os

import pytest

IS_WINDOWS = os.name == "nt"
ROOT = os.path.abspath(os.sep)  # "/" or "C:\\"
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only behaviour (file modes, sh)")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only behaviour")


def same(first, second) -> bool:
    """Path equality as the OS sees it (``shutil.which`` returns ``claude.CMD`` on Windows)."""
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))


def fwd(path) -> str:
    """A path written with ``/``: the form globs and regexes are matched against."""
    return str(path).replace("\\", "/") if IS_WINDOWS else str(path)


def symlink_or_skip(link, target, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without Developer Mode / admin
        pytest.skip(f"symlinks unavailable here: {exc}")
