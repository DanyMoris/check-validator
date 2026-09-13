"""Режим без выписок: настоящий шаблон банка ≠ поступление денег."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkvalidator.bot.storage import Store
from checkvalidator.engine import analyse
from checkvalidator.models import CheckMode, Verdict
from checkvalidator.profiles import ProfileRegistry

ROOT = Path(__file__).resolve().parent.parent
GENUINE = ROOT / "resources" / "genuine"
FORGED = ROOT / "build" / "forged"
VTB_CHECKS = sorted((GENUINE / "VTB" / "Чек").glob("*.pdf"))


@pytest.fixture(scope="module")
def registry() -> ProfileRegistry:
    reg = ProfileRegistry.load()
    if not len(reg):
        pytest.skip("Реестр профилей пуст")
    return reg


@pytest.mark.skipif(not VTB_CHECKS, reason="нет чеков ВТБ")
def test_structure_mode_calls_matching_bank_pdf_genuine(registry: ProfileRegistry) -> None:
    report = analyse(VTB_CHECKS[0], registry, mode=CheckMode.STRUCTURE)
    assert report.verdict is Verdict.GENUINE
    assert report.profile_id == "VTB/Чек"


@pytest.mark.skipif(not VTB_CHECKS, reason="нет чеков ВТБ")
def test_ledger_mode_does_not_call_unsigned_check_confirmed(registry: ProfileRegistry) -> None:
    report = analyse(VTB_CHECKS[0], registry, mode=CheckMode.LEDGER)
    assert report.verdict is Verdict.UNCONFIRMED


def test_structure_mode_flags_office_resave(registry: ProfileRegistry) -> None:
    files = sorted((FORGED / "office_resave" / "VTB" / "Чек").glob("*.pdf"))
    if not files:
        pytest.skip("нет office_resave: сначала tools/forge.py")
    report = analyse(files[0], registry, expected="VTB/Чек", mode=CheckMode.STRUCTURE)
    assert report.verdict is Verdict.FORGED


def test_fraudulent_metadata_is_forged(registry: ProfileRegistry) -> None:
    files = sorted((ROOT / "resources" / "fraudulent").rglob("*.pdf"))
    if not files:
        pytest.skip("нет resources/fraudulent: сначала tools/populate_fraudulent.py")
    missed = [
        p.relative_to(ROOT).as_posix()
        for p in files
        if analyse(p, registry, mode=CheckMode.STRUCTURE).verdict is not Verdict.FORGED
    ]
    assert not missed, f"не пойманы правки метаданных: {missed}"


def test_store_remembers_check_mode(tmp_path: Path) -> None:
    store = Store(tmp_path / "bot.db")
    assert store.get_mode(7) is None
    store.set_mode(7, CheckMode.STRUCTURE)
    assert store.get_mode(7) is CheckMode.STRUCTURE
    store.set_mode(7, CheckMode.LEDGER)
    assert store.get_mode(7) is CheckMode.LEDGER
