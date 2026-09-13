"""Проверки самого детектора.

Главный тест здесь — отсутствие ложных срабатываний: настоящий документ ни при
каких условиях не должен объявляться подделкой. Ошибка в эту сторону дороже
пропуска, потому что по ней человек откажется от настоящих денег.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from checkvalidator.engine import analyse
from checkvalidator.fingerprint import fingerprint
from checkvalidator.models import Severity, Verdict
from checkvalidator.profiles import Profile, ProfileRegistry, Range

ROOT = Path(__file__).resolve().parent.parent
GENUINE = ROOT / "resources" / "genuine"
FORGED = ROOT / "build" / "forged"

GENUINE_FILES = sorted(GENUINE.rglob("*.pdf"))


@pytest.fixture(scope="session")
def registry() -> ProfileRegistry:
    reg = ProfileRegistry.load()
    if not len(reg):
        pytest.skip("Реестр профилей пуст: сначала tools/build_profiles.py")
    return reg


def _declared(path: Path, base: Path, depth: int) -> str | None:
    parts = path.relative_to(base).parts
    return f"{parts[depth]}/{parts[depth + 1]}" if len(parts) > depth + 2 else None


@pytest.mark.skipif(not GENUINE_FILES, reason="нет образцов в resources/genuine")
@pytest.mark.parametrize("pdf", GENUINE_FILES, ids=lambda p: p.name)
def test_genuine_never_flagged_as_forged(pdf: Path, registry: ProfileRegistry) -> None:
    report = analyse(pdf, registry, expected=_declared(pdf, GENUINE, 0))
    assert report.verdict is not Verdict.FORGED, (
        f"ложное срабатывание на настоящем документе: "
        f"{[s.id for s in report.critical_failures]}"
    )


@pytest.mark.skipif(not GENUINE_FILES, reason="нет образцов")
@pytest.mark.parametrize("pdf", GENUINE_FILES, ids=lambda p: p.name)
def test_genuine_matches_its_own_profile(pdf: Path, registry: ProfileRegistry) -> None:
    expected = _declared(pdf, GENUINE, 0)
    report = analyse(pdf, registry)
    assert report.profile_id == expected


@pytest.mark.skipif(not GENUINE_FILES, reason="нет образцов")
def test_no_genuine_document_is_pure_raster() -> None:
    """Банковский PDF всегда содержит текст. Если это перестанет быть правдой,
    универсальная проверка text_layer начнёт давать ложные срабатывания."""
    for pdf in GENUINE_FILES:
        fp = fingerprint(pdf)
        assert fp.fonts, f"{pdf.name}: нет ни одного шрифта"


def _forged(attack: str) -> list[Path]:
    d = FORGED / attack
    return sorted(d.rglob("*.pdf")) if d.exists() else []


@pytest.mark.parametrize("attack", ["append", "rasterise"])
def test_structural_attacks_are_caught(attack: str, registry: ProfileRegistry) -> None:
    """Атаки, меняющие устройство файла, должны ловиться полностью."""
    files = _forged(attack)
    if not files:
        pytest.skip("нет подделок: сначала tools/forge.py")
    missed = [
        p.name
        for p in files
        if analyse(p, registry, expected=_declared(p, FORGED / attack, 0)).verdict
        is not Verdict.FORGED
    ]
    assert not missed, f"пропущены подделки вида {attack}: {missed}"


def test_unknown_document_is_not_called_forged(tmp_path: Path, registry: ProfileRegistry) -> None:
    """Незнакомый банк — это «не знаю», а не «подделка».

    Иначе сервис начнёт обвинять в мошенничестве всех, чей банк мы ещё не успели
    внести в реестр.
    """
    stub = tmp_path / "unknown.pdf"
    stub.write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"/BaseFont /SomeUnknownFont\ntrailer\n<< >>\nstartxref\n0\n%%EOF\n"
    )
    report = analyse(stub, registry)
    assert report.verdict is Verdict.UNCONFIRMED
    assert report.profile_id is None


def test_unsigned_genuine_is_not_confirmed(
    registry: ProfileRegistry,
) -> None:
    """Чек без электронной подписи не может стать «ПОДТВЕРЖДЁН»."""
    unsigned = [
        p
        for p in GENUINE_FILES
        if not (p.parent.name == "Справка" and p.parent.parent.name == "VTB")
    ]
    if not unsigned:
        pytest.skip("нет неподписанных образцов")
    for pdf in unsigned:
        assert analyse(pdf, registry).verdict is not Verdict.CONFIRMED


def test_provisional_profile_softens_strict_checks() -> None:
    """У профиля с одним образцом строгие проверки не должны быть критическими."""
    from checkvalidator.checks import check_producer

    base = dict(
        bank="X",
        doc_type="Чек",
        pdf_versions=["1.4"],
        objects=Range(10, 10),
        producers=["real"],
        font_sets=[["F"]],
        subtype_sets=[["Type0"]],
        has_image=[False],
        signature_expected=False,
        revisions=Range(1, 1),
        size=Range(100, 200),
    )
    thin = Profile(id="X/Чек", samples=1, **base)
    solid = Profile(id="X/Чек", samples=5, **base)

    class FakeFp:
        producer = "something else"

    assert check_producer(FakeFp, thin).severity is Severity.WARN
    assert check_producer(FakeFp, solid).severity is Severity.CRITICAL
