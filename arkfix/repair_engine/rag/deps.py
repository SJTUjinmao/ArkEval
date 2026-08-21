from __future__ import annotations

import sys
from pathlib import Path


def arkeval_root() -> Path:
    return Path(__file__).resolve().parents[3]


def ensure_arkeval_on_path() -> Path:
    root = arkeval_root()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root

