"""Проверка электронной подписи ГОСТ на справках ВТБ."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkvalidator.engine import analyse
from checkvalidator.gostsig import verify_pdf_gost
from checkvalidator.models import Verdict
from checkvalidator.profiles import ProfileRegistry

ROOT = Path(__file__).resolve().parent.parent
VTB_CERTS = sorted((ROOT / "resources" / "genuine" / "VTB" / "Справка").glob("*.pdf"))


@pytest.fixture(scope="module")
def registry() -> ProfileRegistry:
    return ProfileRegistry.load()


@pytest.mark.skipif(not VTB_CERTS, reason="нет справок ВТБ")
def test_at_least_one_vtb_certificate_is_confirmed(registry: ProfileRegistry) -> None:
    confirmed = [p.name for p in VTB_CERTS if analyse(p, registry).verdict is Verdict.CONFIRMED]
    assert confirmed, "ни одна справка ВТБ не прошла проверку подписи"


@pytest.mark.skipif(not VTB_CERTS, reason="нет справок ВТБ")
def test_vtb_signer_is_the_bank() -> None:
    ok = [verify_pdf_gost(p) for p in VTB_CERTS if verify_pdf_gost(p).crypto_ok]
    assert ok
    for result in ok:
        assert result.signer_inn == "7702070139"
        assert result.trusted_signer


@pytest.mark.skipif(not VTB_CERTS, reason="нет справок ВТБ")
def test_tampered_body_is_not_confirmed(tmp_path: Path, registry: ProfileRegistry) -> None:
    """Правка байта в теле файла ломает совпадение с подписанным хешем."""
    source = next((p for p in VTB_CERTS if verify_pdf_gost(p).ok), None)
    if source is None:
        pytest.skip("нет справки с полностью сходящейся подписью")
    data = bytearray(source.read_bytes())
    data[200] ^= 0xFF
    dest = tmp_path / "tampered.pdf"
    dest.write_bytes(data)
    report = analyse(dest, registry, expected="VTB/Справка")
    assert report.verdict is not Verdict.CONFIRMED
    digest = next(s for s in report.signals if s.id == "gost_digest")
    assert digest.failed
