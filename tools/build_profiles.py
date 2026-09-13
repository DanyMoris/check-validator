"""Собирает profiles/profiles.yaml из образцов в resources/genuine/.

Профиль строится как объединение того, что реально встретилось в образцах:
все виденные producer'ы, наборы шрифтов, диапазон числа объектов и т.д. Поля
notes и quirks, если их дописали руками, при пересборке сохраняются.

Запуск:  .venv\\Scripts\\python.exe tools\\build_profiles.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from checkvalidator.fingerprint import fingerprint  # noqa: E402
from checkvalidator.profiles import Profile, ProfileRegistry, Range  # noqa: E402

GENUINE = ROOT / "resources" / "genuine"


def build() -> tuple[ProfileRegistry, list[str]]:
    previous = ProfileRegistry.load()
    warnings: list[str] = []
    grouped: dict[tuple[str, str], list] = defaultdict(list)

    for pdf in sorted(GENUINE.rglob("*.pdf")):
        rel = pdf.relative_to(GENUINE)
        if len(rel.parts) < 3:
            warnings.append(f"{rel.as_posix()}: лежит не в <БАНК>/<ТИП>/, пропущен")
            continue
        bank, doc_type = rel.parts[0], rel.parts[1]
        try:
            grouped[(bank, doc_type)].append(fingerprint(pdf))
        except Exception as exc:
            warnings.append(f"{rel.as_posix()}: не удалось разобрать — {exc}")

    profiles: dict[str, Profile] = {}
    for (bank, doc_type), fps in sorted(grouped.items()):
        pid = f"{bank}/{doc_type}"
        objects = [f.objects for f in fps]
        sizes = [f.size for f in fps]
        revisions = [f.revisions for f in fps]

        font_sets = sorted({tuple(f.fonts) for f in fps})
        subtype_sets = sorted({tuple(f.subtypes) for f in fps})
        producers = sorted({f.producer for f in fps}, key=lambda x: (x is None, x))
        signed = [f.signature.present for f in fps]

        if len(set(objects)) > 1 and len(fps) >= 3:
            warnings.append(
                f"{pid}: число объектов не постоянно ({sorted(set(objects))}) — "
                "видимо, зависит от содержимого; как признак оно слабое"
            )
        if len(set(signed)) > 1:
            warnings.append(
                f"{pid}: часть образцов подписана, часть нет — возможно, это два разных типа"
            )

        kept = previous.get(pid)
        profiles[pid] = Profile(
            id=pid,
            bank=bank,
            doc_type=doc_type,
            samples=len(fps),
            pdf_versions=sorted({f.pdf_version for f in fps}),
            objects=Range(min(objects), max(objects)),
            producers=list(producers),
            font_sets=[list(fs) for fs in font_sets],
            subtype_sets=[list(ss) for ss in subtype_sets],
            has_image=sorted({f.has_image for f in fps}),
            signature_expected=all(signed),
            revisions=Range(min(revisions), max(revisions)),
            size=Range(min(sizes), max(sizes)),
            notes=kept.notes if kept else "",
            quirks=kept.quirks if kept else _auto_quirks(fps),
        )

    return ProfileRegistry(profiles), warnings


def _auto_quirks(fps: list) -> list[str]:
    """Особенности, о которых стоит помнить, читая профиль."""
    quirks = []
    if all(f.signature.present for f in fps):
        alg = next((f.signature.signature_algorithm for f in fps if f.signature.present), None)
        quirks.append(
            f"Документ подписан электронной подписью ({alg}); вторая ревизия файла — "
            "это сама подпись, а не следы правки"
        )
    if all(not f.has_image for f in fps):
        quirks.append("Внутри нет ни одной картинки — для этого шаблона это норма")
    return quirks


def main() -> None:
    registry, warnings = build()
    if not len(registry):
        sys.exit(f"В {GENUINE} не найдено образцов")

    path = registry.save()
    print(f"Собрано профилей: {len(registry)}  ->  {path.relative_to(ROOT).as_posix()}\n")

    for p in sorted(registry, key=lambda x: x.id):
        flag = "" if p.established else "  ПРЕДВАРИТЕЛЬНЫЙ (мало образцов)"
        print(f"  {p.id:<20} образцов: {p.samples}   объектов: "
              f"{p.objects.min}-{p.objects.max}   подпись: "
              f"{'да' if p.signature_expected else 'нет'}{flag}")

    thin = [p for p in registry if not p.established]
    if thin:
        print("\nДоберите образцы (нужно от 3 на каждый тип):")
        for p in sorted(thin, key=lambda x: x.id):
            print(f"  {p.id} — сейчас {p.samples}")

    if warnings:
        print("\nПредупреждения:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
