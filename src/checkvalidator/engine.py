"""Правила вынесения вердикта.

Все решающие правила собраны здесь, чтобы их можно было прочесть подряд и
пересмотреть, не трогая сами проверки.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .checks import run_gost, run_semantic, run_structural, run_universal
from .extract import (
    ExtractedFields,
    document_direction,
    extract,
    extract_pdf_text,
    format_dt,
    format_rub,
    income_hint,
)
from .fingerprint import Fingerprint, fingerprint
from .gostsig import verify_pdf_gost
from .ledger import Ledger, MatchResult, match_signals
from .models import CheckMode, Report, Severity, Signal, Verdict
from .profiles import Profile, ProfileRegistry

WARN_ESCALATION_THRESHOLD = 3
"""Столько несерьёзных расхождений подряд уже не бывают случайностью."""


def _synthetic(id_: str, text: str, **evidence) -> Signal:
    return Signal(
        id=id_,
        passed=False,
        severity=Severity.CRITICAL,
        explanation=text,
        evidence=evidence,
    )


def _apply_composite_rules(signals: list[Signal]) -> list[Signal]:
    """Правила, которые смотрят на сочетание признаков, а не на каждый по отдельности."""
    extra: list[Signal] = []
    failed = {s.id for s in signals if s.failed}

    # Ни программа-создатель, ни внутреннее устройство не совпали. По отдельности
    # каждое объяснимо обновлением шаблона банка, вместе — почти наверняка подделка.
    if {"producer", "objects"} <= failed:
        extra.append(
            _synthetic(
                "template_mismatch",
                "Документ не соответствует банковскому шаблону сразу по двум "
                "независимым признакам: программа-создатель и внутреннее устройство файла",
            )
        )

    warn_failures = [s for s in signals if s.failed and s.severity is Severity.WARN]
    if len(warn_failures) >= WARN_ESCALATION_THRESHOLD and "template_mismatch" not in {
        s.id for s in extra
    }:
        extra.append(
            _synthetic(
                "many_discrepancies",
                f"Слишком много расхождений с образцом ({len(warn_failures)}): "
                + "; ".join(s.id for s in warn_failures),
                count=len(warn_failures),
            )
        )

    return extra


def _summarise(
    verdict: Verdict,
    report_signals: list[Signal],
    profile: Profile | None,
    fields: ExtractedFields | None,
    match: MatchResult | None,
) -> str:
    if verdict is Verdict.FORGED:
        reasons = [
            s.explanation.rstrip(". ")
            for s in report_signals
            if s.failed and s.severity is Severity.CRITICAL
        ]
        return "Документ не прошёл проверку. " + ". ".join(reasons) + "."
    if verdict is Verdict.GENUINE:
        who = next(
            (s.evidence.get("signer") for s in report_signals if s.id == "gost_crypto" and s.passed),
            None,
        )
        if who:
            return (
                f"Документ банка: электронная подпись «{who}» сходится, файл не меняли. "
                "Это про подлинность PDF, не про зачисление денег на счёт."
            )
        return (
            "Документ совпадает с шаблоном банка: его собрала та же программа, "
            "файл не пересохраняли в Word, «печать в PDF» и подобных программах. "
            "Это не проверка, что деньги пришли — выписок в этом режиме нет."
        )
    if verdict is Verdict.CONFIRMED:
        if match is not None and match.confirmed and match.hit is not None:
            return (
                f"На счёте найдено поступление {format_rub(match.hit.amount_kopecks)} "
                f"за {format_dt(match.hit.occurred_at)}. Это подтверждает, что деньги пришли."
            )
        who = next(
            (s.evidence.get("signer") for s in report_signals if s.id == "gost_crypto" and s.passed),
            None,
        )
        if who:
            return (
                f"Подлинность документа подтверждена электронной подписью «{who}». "
                "Файл с момента подписания банком не изменялся."
            )
        return "Подлинность документа подтверждена электронной подписью банка."
    extra = ""
    if match is not None and not match.confirmed:
        outgoing = any(s.id == "ledger_outgoing" for s in report_signals)
        missing = any(s.id == "ledger_missing" for s in report_signals)
        if outgoing:
            extra = (
                " Документ похож на исходящий перевод; журнал сверяет только "
                "полученные деньги, совпадения нет."
            )
        elif missing:
            extra = " Совпадения с журналом поступлений нет — это не делает файл подделкой."
    if fields is not None and not fields.usable_for_match:
        extra += " Сумму или дату прочитать не удалось — сверка со счётом не выполнялась."
    elif fields is not None and (match is None or not match.confirmed):
        hint = income_hint(fields)
        if hint and "исходящий" not in extra:
            extra += (
                f" Чтобы подтвердить, что деньги пришли, внесите поступление: {hint} "
                "— или пришлите выписку командой /statement."
            )
    if profile is None:
        return (
            "Признаков подделки не обнаружено, но эталон для такого документа "
            "пока не собран, поэтому проверка была неполной. Подтвердить поступление "
            "денег этот результат не может."
            + extra
        )
    return (
        "Явных признаков подделки не обнаружено. Это не подтверждает, что перевод "
        "состоялся: убедиться в поступлении денег можно только сверкой со счётом."
        + extra
    )


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyse(
    path: str | Path,
    registry: ProfileRegistry | None = None,
    expected: str | None = None,
    ledger: Ledger | None = None,
    consume: bool = False,
    doc_hash: str | None = None,
    mode: CheckMode | str = CheckMode.LEDGER,
) -> Report:
    """Проверяет документ.

    expected — если отправитель заявил, чей это документ («VTB/Чек»), то
    несоответствие именно этому эталону становится доказательством подделки. Без
    такого заявления неопознанный документ остаётся просто неопознанным: у нас
    нет способа отличить подделку от банка, эталон которого мы ещё не собрали.

    mode=LEDGER (по умолчанию) — сверка с журналом: совпадение суммы и даты даёт
    ПОДТВЕРЖДЁН; нет прихода при живой структуре — НЕ ПОДТВЕРЖДЁН, не подделка.

    mode=STRUCTURE — журнала нет. Шаблон банка совпал и критических следов
    чужой программы нет → ПОДЛИННЫЙ. Файл пересохранили (Word, печать в PDF) →
    ПОДДЕЛКА. Неопознанный банк по-прежнему НЕ ПОДТВЕРЖДЁН: это не обвинение.

    consume=True помечает найденную запись журнала, чтобы один платёж не
    подтвердил два разных файла.
    """
    registry = registry if registry is not None else ProfileRegistry.load()
    fp: Fingerprint = fingerprint(path)
    profile, _candidates = registry.match(fp)
    fields = extract(path)

    # Проверки, верные для любого банковского документа, выполняются всегда —
    # даже если банк не опознан.
    signals: list[Signal] = run_universal(fp)
    signals.extend(run_semantic(fields))

    declared = registry.get(expected) if expected else None
    if expected and declared is None:
        signals.append(
            Signal(
                id="declared_profile",
                passed=False,
                severity=Severity.INFO,
                explanation=f"Эталона «{expected}» в реестре нет; сверять не с чем",
                evidence={"requested": expected, "known": registry.ids()},
            )
        )
    elif declared is not None and (profile is None or profile.id != declared.id):
        signals.append(
            Signal(
                id="declared_profile",
                passed=False,
                severity=Severity.CRITICAL,
                explanation=(
                    f"Документ выдаётся за «{declared.id}», но не совпадает с этим "
                    f"эталоном" + (f"; больше похож на «{profile.id}»" if profile else "")
                ),
                evidence={"declared": declared.id, "matched": profile.id if profile else None},
            )
        )
        profile = declared  # дальше проверяем против заявленного эталона

    if profile is None:
        signals.append(
            Signal(
                id="profile_known",
                passed=False,
                severity=Severity.INFO,
                explanation=(
                    "Банк и тип документа не опознаны: подходящего эталона нет. "
                    "Выполнены только общие проверки. Если известно, чей это документ, "
                    "укажите это явно — сверка с конкретным эталоном строже."
                ),
                evidence={
                    "producer": fp.producer,
                    "fonts": fp.fonts,
                    "known_profiles": registry.ids(),
                },
            )
        )
    else:
        signals.append(
            Signal(
                id="profile_known",
                passed=True,
                severity=Severity.INFO,
                explanation=f"Документ опознан как «{profile.id}»"
                + ("" if profile.established else " (эталон предварительный, мало образцов)"),
                evidence={"profile": profile.id, "confidence": profile.confidence},
            )
        )
        signals.extend(run_structural(fp, profile))
        signals.extend(_apply_composite_rules(signals))

    gost = verify_pdf_gost(path) if fp.signature.present else None
    if gost is not None:
        signals.extend(run_gost(gost))

    mode = mode if isinstance(mode, CheckMode) else CheckMode(mode)

    match: MatchResult | None = None
    digest = doc_hash
    direction = "unknown"
    try:
        direction = document_direction(extract_pdf_text(path))
    except Exception:  # noqa: BLE001
        direction = "unknown"
    use_ledger = mode is CheckMode.LEDGER and ledger is not None
    if use_ledger:
        assert ledger is not None
        if digest is None:
            digest = _file_sha256(path)
        match = ledger.match(fields, digest)
        signals.extend(match_signals(match, fields, direction=direction))
        if consume and match.confirmed and match.hit is not None:
            ledger.consume(match.hit.id, digest)

    signed_ok = gost is not None and gost.ok
    money_ok = match is not None and match.confirmed
    critical = any(s.failed and s.severity is Severity.CRITICAL for s in signals)
    if mode is CheckMode.STRUCTURE:
        if critical:
            verdict = Verdict.FORGED
        elif signed_ok or profile is not None:
            verdict = Verdict.GENUINE
        else:
            verdict = Verdict.UNCONFIRMED
    elif signed_ok or money_ok:
        verdict = Verdict.CONFIRMED
    elif critical:
        verdict = Verdict.FORGED
    else:
        verdict = Verdict.UNCONFIRMED

    return Report(
        verdict=verdict,
        signals=signals,
        profile_id=profile.id if profile else None,
        summary=_summarise(verdict, signals, profile, fields, match),
        fields=fields,
        match=match,
    )
