"""测试公共设置：把账本落到临时目录，避免污染仓库里的 data/ledger.json。"""
from __future__ import annotations

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ledger-test-")
os.environ["LEDGER_DATA_DIR"] = _TEST_DATA_DIR

import pytest  # noqa: E402

from app.services.ledger import LedgerService, ledger_service  # noqa: E402


@pytest.fixture(autouse=True)
def clean_ledger() -> None:
    ledger_service.reset()


@pytest.fixture
def service(tmp_path) -> LedgerService:
    """独立账本实例：用于验证重启后仍能读到同一份截面。"""
    return LedgerService(path=str(tmp_path / "ledger.json"))
