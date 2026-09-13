"""Структурные проверки: сверка отпечатка файла с эталонным профилем банка.

Каждое замечание — отдельный Signal. Решение о вердикте принимается не здесь,
а в engine.py, чтобы правила были видны в одном месте.

Важно: все пороги берутся из профиля и нигде не зашиты глобально. Признак,
верный для одного шаблона, для другого бывает ровно наоборот — например, чек ВТБ
не содержит ни одной картинки, а справка Сбера содержит их всегда.
"""

from __future__ import annotations

from .extract import ExtractedFields, format_dt, format_rub
from .fingerprint import Fingerprint
from .gostsig import GostVerifyResult
from .models import Severity, Signal
from .profiles import Profile
from .requisites import account_key_ok, inn_checksum_ok

SIZE_TOLERANCE = 0.45
"""Размер сильно зависит от длины содержимого, поэтому допуск широкий."""


def _sig(id_: str, passed: bool, sev: Severity, text: str, **evidence) -> Signal:
    return Signal(id=id_, passed=passed, severity=sev, explanation=text, evidence=evidence)


def check_producer(fp: Fingerprint, p: Profile) -> Signal:
    """Чем сформирован файл.

    Банк выпускает документы одного типа всегда одной и той же программой, так
    что чужая строка producer означает: перед нами не оригинал банка, а файл,
    прошедший через что-то ещё. Даже если содержимое при этом не тронули,
    поручиться за такой файл нельзя — правку он бы пронёс так же незаметно.

    Риск обратной стороны: если банк сменит свою программу, все новые настоящие
    документы начнут не проходить проверку. Увидим это сразу — посыплется весь
    поток, а не отдельные документы, — и пересоберём профиль.
    """
    ok = fp.producer in p.producers
    expected = ", ".join(str(x) for x in p.producers)
    return _sig(
        "producer",
        ok,
        Severity.CRITICAL if p.established else Severity.WARN,
        "Программа-создатель файла совпадает с банковской"
        if ok
        else f"Файл создан не той программой: у настоящего «{expected}», здесь «{fp.producer}»",
        expected=p.producers,
        actual=fp.producer,
        profile_confidence=p.confidence,
    )


def check_pdf_version(fp: Fingerprint, p: Profile) -> Signal:
    ok = fp.pdf_version in p.pdf_versions
    return _sig(
        "pdf_version",
        ok,
        Severity.WARN,
        "Версия формата PDF обычная для этого документа"
        if ok
        else f"Необычная версия PDF: ожидалась {'/'.join(p.pdf_versions)}, в файле {fp.pdf_version}",
        expected=p.pdf_versions,
        actual=fp.pdf_version,
    )


def check_objects(fp: Fingerprint, p: Profile) -> Signal:
    # У непроверенного профиля точное число объектов ещё не подтверждено образцами.
    tolerance = 0 if p.established else 6
    ok = p.objects.contains(fp.objects, tolerance)
    rng = f"{p.objects.min}" if p.objects.min == p.objects.max else f"{p.objects.min}–{p.objects.max}"
    return _sig(
        "objects",
        ok,
        Severity.WARN,
        "Внутреннее устройство файла соответствует шаблону"
        if ok
        else f"Не сходится число внутренних элементов: у настоящего {rng}, здесь {fp.objects}",
        expected=rng,
        actual=fp.objects,
        profile_confidence=p.confidence,
    )


def check_fonts(fp: Fingerprint, p: Profile) -> Signal:
    known = [sorted(fs) for fs in p.font_sets]
    ok = sorted(fp.fonts) in known
    return _sig(
        "fonts",
        ok,
        Severity.WARN,
        "Набор шрифтов совпадает с банковским"
        if ok
        else f"Другой набор шрифтов: ожидался {known}, в файле {sorted(fp.fonts)}",
        expected=known,
        actual=sorted(fp.fonts),
    )


def check_subtypes(fp: Fingerprint, p: Profile) -> Signal:
    known = [sorted(ss) for ss in p.subtype_sets]
    ok = sorted(fp.subtypes) in known
    return _sig(
        "subtypes",
        ok,
        Severity.INFO,
        "Состав объектов документа обычный"
        if ok
        else f"Необычный состав объектов: {sorted(fp.subtypes)}",
        expected=known,
        actual=sorted(fp.subtypes),
    )


def check_images(fp: Fingerprint, p: Profile) -> Signal:
    ok = fp.has_image in p.has_image
    if ok:
        text = "Наличие картинок внутри файла соответствует шаблону"
    elif fp.has_image:
        text = "В документе есть картинки, хотя настоящий такого типа их не содержит"
    else:
        text = "В документе нет картинок, хотя настоящий такого типа их всегда содержит"
    return _sig("images", ok, Severity.WARN, text, expected=p.has_image, actual=fp.has_image)


def check_revisions(fp: Fingerprint, p: Profile) -> Signal:
    """Дописывание в уже готовый файл.

    Само по себе наличие второй ревизии не является нарушением: подписанный PDF
    всегда состоит минимум из двух — вторую добавляет сама электронная подпись.
    Нарушение — это ревизий БОЛЬШЕ, чем бывает у настоящего документа.
    """
    ok = fp.revisions <= p.revisions.max
    return _sig(
        "revisions",
        ok,
        Severity.CRITICAL,
        "Следов дописывания в готовый файл нет"
        if ok
        else f"Файл дописывали после создания: ревизий {fp.revisions}, "
        f"у настоящего не больше {p.revisions.max}",
        expected=p.revisions.max,
        actual=fp.revisions,
    )


def check_signature_presence(fp: Fingerprint, p: Profile) -> Signal:
    s = fp.signature
    if not p.signature_expected:
        return _sig(
            "signature_presence",
            True,
            Severity.INFO,
            "Документ этого типа банк не подписывает — отсутствие подписи нормально",
            expected=False,
            actual=s.present,
        )
    return _sig(
        "signature_presence",
        s.present,
        Severity.CRITICAL,
        f"Электронная подпись на месте ({s.signature_algorithm or 'алгоритм не определён'})"
        if s.present
        else "Электронной подписи нет, хотя документ этого типа банк всегда подписывает",
        expected=True,
        actual=s.present,
    )


def check_size(fp: Fingerprint, p: Profile) -> Signal:
    lo = int(p.size.min * (1 - SIZE_TOLERANCE))
    hi = int(p.size.max * (1 + SIZE_TOLERANCE))
    ok = lo <= fp.size <= hi
    return _sig(
        "size",
        ok,
        Severity.INFO,
        "Размер файла в обычных пределах"
        if ok
        else f"Необычный размер файла: {fp.size} байт при ожидаемых {lo}–{hi}",
        expected=[lo, hi],
        actual=fp.size,
    )


# --- Проверки, не зависящие от эталона ------------------------------------
# Верны для любого банковского документа, поэтому выполняются всегда — в том
# числе когда банк не опознан. Только благодаря им подделку видно и тогда,
# когда она не похожа вообще ни на один известный шаблон.


def check_has_text_layer(fp: Fingerprint) -> Signal:
    """Банк всегда выдаёт документ с текстом, а не картинку.

    Если шрифтов нет вовсе, а картинка есть — перед нами страница, отрисованная
    в изображение и завёрнутая обратно в PDF. Так поступают, когда правят
    документ в графическом редакторе.
    """
    is_raster = not fp.fonts and fp.has_image
    return _sig(
        "text_layer",
        not is_raster,
        Severity.CRITICAL,
        "В документе есть текстовый слой"
        if not is_raster
        else "Документ представляет собой картинку без текста: банк такие не выдаёт. "
        "Похоже, страницу отрисовали в изображение — обычно чтобы скрыть правку",
        fonts=fp.fonts,
        has_image=fp.has_image,
    )


def check_signature_intact(fp: Fingerprint) -> Signal | None:
    """Если подпись есть, она обязана покрывать файл целиком."""
    s = fp.signature
    if not s.present:
        return None
    if s.parse_error and s.covers_whole_file is None:
        return _sig(
            "signature_intact",
            False,
            Severity.WARN,
            f"В документе есть электронная подпись, но разобрать её не удалось "
            f"({s.parse_error})",
            error=s.parse_error,
        )
    if s.covers_whole_file is None:
        return None
    return _sig(
        "signature_intact",
        s.covers_whole_file,
        Severity.CRITICAL,
        "Электронная подпись цела и покрывает весь документ"
        if s.covers_whole_file
        else "Документ изменяли после подписания — подпись больше не покрывает файл целиком",
        covers_whole_file=s.covers_whole_file,
    )


def check_not_encrypted(fp: Fingerprint) -> Signal:
    return _sig(
        "not_encrypted",
        not fp.encrypted,
        Severity.WARN,
        "Файл не зашифрован"
        if not fp.encrypted
        else "Файл зашифрован — банковские чеки и справки так не выдают",
        encrypted=fp.encrypted,
    )


UNIVERSAL_CHECKS = (
    check_has_text_layer,
    check_signature_intact,
    check_not_encrypted,
)


def run_universal(fp: Fingerprint) -> list[Signal]:
    return [s for s in (c(fp) for c in UNIVERSAL_CHECKS) if s is not None]


def run_gost(result: GostVerifyResult) -> list[Signal]:
    """Результаты проверки электронной подписи — отдельные сигналы."""
    if not result.present:
        return []
    if result.error:
        return [
            _sig(
                "gost_parse",
                False,
                Severity.WARN,
                f"Электронная подпись есть, но разобрать её не удалось ({result.error})",
                error=result.error,
            )
        ]

    who = result.signer_name or "неизвестный подписант"
    inn = f", ИНН {result.signer_inn}" if result.signer_inn else ""
    issuer = f" Кем выдан сертификат: {result.issuer_name}." if result.issuer_name else ""

    return [
        _sig(
            "gost_crypto",
            result.crypto_ok,
            Severity.CRITICAL,
            f"Электронная подпись {who}{inn} математически верна.{issuer}"
            if result.crypto_ok
            else "Электронная подпись не сходится с сертификатом — её могли подделать или повредить",
            signer=result.signer_name,
            inn=result.signer_inn,
            issuer=result.issuer_name,
        ),
        _sig(
            "gost_digest",
            result.digest_matches,
            Severity.WARN if result.crypto_ok else Severity.CRITICAL,
            "Содержимое файла совпадает с тем, что банк подписал"
            if result.digest_matches
            else "Содержимое файла не совпадает с подписанным снимком: после выпуска документ могли изменить",
        ),
        _sig(
            "gost_coverage",
            result.covers_file,
            Severity.CRITICAL,
            "Подпись покрывает документ целиком"
            if result.covers_file
            else "Подпись не покрывает файл целиком — после подписания что-то дописали",
        ),
        _sig(
            "gost_signer",
            result.trusted_signer,
            Severity.CRITICAL if result.crypto_ok else Severity.WARN,
            f"Подписант в списке доверенных банков ({who}{inn})"
            if result.trusted_signer
            else f"Подписант не из списка доверенных банков ({who}{inn})",
            inn=result.signer_inn,
        ),
    ]


STRUCTURAL_CHECKS = (
    check_producer,
    check_pdf_version,
    check_objects,
    check_fonts,
    check_subtypes,
    check_images,
    check_revisions,
    check_signature_presence,
    check_size,
)


def run_structural(fp: Fingerprint, profile: Profile) -> list[Signal]:
    signals = []
    for check in STRUCTURAL_CHECKS:
        result = check(fp, profile)
        if result is not None:
            signals.append(result)
    return signals


def run_semantic(fields: ExtractedFields) -> list[Signal]:
    """Проверки содержимого, не зависящие от эталона файла."""
    signals: list[Signal] = []

    if fields.amount_kopecks is not None:
        when = ""
        if fields.occurred_at is not None:
            when = f", дата {format_dt(fields.occurred_at)}"
        signals.append(
            _sig(
                "extracted_amount",
                True,
                Severity.INFO,
                f"В документе сумма {format_rub(fields.amount_kopecks)}{when}",
                amount_kopecks=fields.amount_kopecks,
            )
        )
    else:
        signals.append(
            _sig(
                "extracted_amount",
                False,
                Severity.INFO,
                "Не удалось прочитать сумму — сверка со счётом для этого файла невозможна",
            )
        )

    if fields.inn:
        ok = inn_checksum_ok(fields.inn)
        signals.append(
            _sig(
                "inn_checksum",
                ok,
                Severity.CRITICAL,
                "Контрольная сумма ИНН сходится"
                if ok
                else "ИНН в документе не проходит проверку контрольной суммы — такого номера не бывает",
                inn_len=len(fields.inn),
            )
        )

    if fields.bik and fields.account:
        ok = account_key_ok(fields.bik, fields.account)
        signals.append(
            _sig(
                "account_key",
                ok,
                Severity.CRITICAL,
                "Ключ расчётного счёта сходится с БИК"
                if ok
                else "Расчётный счёт не сходится с БИК — реквизиты собраны с ошибкой или выдуманы",
                bik=fields.bik,
            )
        )

    if (
        fields.amount_kopecks is not None
        and fields.commission_kopecks is not None
        and fields.total_kopecks is not None
    ):
        expected = fields.amount_kopecks + fields.commission_kopecks
        ok = expected == fields.total_kopecks
        signals.append(
            _sig(
                "commission_math",
                ok,
                Severity.CRITICAL,
                "Сумма платежа и комиссия сходятся с итогом"
                if ok
                else (
                    "Итого не равно сумме платежа и комиссии: "
                    f"{format_rub(fields.amount_kopecks)} + {format_rub(fields.commission_kopecks)} "
                    f"≠ {format_rub(fields.total_kopecks)}"
                ),
                expected=expected,
                actual=fields.total_kopecks,
            )
        )

    return signals
