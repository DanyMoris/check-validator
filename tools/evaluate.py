"""Прогоняет детектор по настоящим документам и по смоделированным подделкам.

Две цифры, за которыми стоит следить:
  - ложные срабатывания: настоящий документ признан подделкой. Самая дорогая ошибка.
  - пропуски: подделка прошла проверку.

Запуск:  .venv\\Scripts\\python.exe tools\\evaluate.py
Перед этим: build_profiles.py и forge.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from checkvalidator.engine import analyse  # noqa: E402
from checkvalidator.models import Verdict  # noqa: E402
from checkvalidator.profiles import ProfileRegistry  # noqa: E402
from forge import HARD_BY_DESIGN  # noqa: E402

GENUINE = ROOT / "resources" / "genuine"
FORGED = ROOT / "build" / "forged"


def main() -> None:
    registry = ProfileRegistry.load()
    if not len(registry):
        sys.exit("Нет профилей. Сначала: tools/build_profiles.py")

    genuine = sorted(GENUINE.rglob("*.pdf"))
    if not genuine:
        sys.exit(f"Нет образцов в {GENUINE}")

    print("НАСТОЯЩИЕ ДОКУМЕНТЫ")
    false_positives = []
    for p in genuine:
        r = analyse(p, registry)
        if r.verdict is Verdict.FORGED:
            false_positives.append((p, r))
    print(f"  всего: {len(genuine)}   ложных срабатываний: {len(false_positives)}")
    for p, r in false_positives:
        reasons = ", ".join(s.id for s in r.critical_failures)
        print(f"    ЛОЖНОЕ СРАБАТЫВАНИЕ  {p.relative_to(GENUINE).as_posix()}  ({reasons})")

    if not FORGED.exists():
        print("\nПодделок нет. Сначала: tools/forge.py")
        return

    print("\nСМОДЕЛИРОВАННЫЕ ПОДДЕЛКИ")
    by_attack: dict[str, Counter] = defaultdict(Counter)
    missed: dict[str, list[str]] = defaultdict(list)
    triggers: dict[str, Counter] = defaultdict(Counter)

    for p in sorted(FORGED.rglob("*.pdf")):
        parts = p.relative_to(FORGED).parts
        attack = parts[0]
        # Подделку всегда выдают за конкретный документ, поэтому и проверяем её
        # против того эталона, за который её выдают.
        declared = f"{parts[1]}/{parts[2]}" if len(parts) >= 4 else None
        r = analyse(p, registry, expected=declared)
        caught = r.verdict is Verdict.FORGED
        by_attack[attack]["caught" if caught else "missed"] += 1
        if caught:
            for s in r.critical_failures:
                triggers[attack][s.id] += 1
        else:
            missed[attack].append(p.relative_to(FORGED / attack).as_posix())

    total_caught = total = 0
    for attack in sorted(by_attack):
        c = by_attack[attack]["caught"]
        n = c + by_attack[attack]["missed"]
        total_caught += c
        total += n
        pct = 100 * c / n if n else 0
        note = "  предел метода" if attack in HARD_BY_DESIGN else ""
        top = ", ".join(f"{k}×{v}" for k, v in triggers[attack].most_common(3))
        print(f"  {attack:<16} поймано {c}/{n}  ({pct:.0f}%){note}   {top}")

    print(f"\n  итого поймано: {total_caught}/{total} ({100*total_caught/total:.0f}%)")

    # Отдельно: сколько пропусков объясняется нехваткой образцов, а не слабостью
    # проверок. По предварительному профилю строгие проверки намеренно смягчены.
    thin = {p.id for p in registry if not p.established}
    thin_misses = sum(
        1
        for attack, files in missed.items()
        if attack not in HARD_BY_DESIGN
        for f in files
        if "/".join(f.split("/")[:2]) in thin
    )
    solvable = sum(
        len(files) for attack, files in missed.items() if attack not in HARD_BY_DESIGN
    )

    if missed:
        print("\n  ПРОПУЩЕНЫ:")
        for attack, files in sorted(missed.items()):
            tag = "  (предел метода)" if attack in HARD_BY_DESIGN else ""
            for f in files:
                mark = "  ← мало образцов" if "/".join(f.split("/")[:2]) in thin else ""
                print(f"    {attack}/{f}{tag}{mark}")

    print("\nЧто из этого следует")
    if thin_misses:
        print(
            f"  {thin_misses} из {solvable} исправимых пропусков — это профили, где "
            f"меньше трёх образцов: {', '.join(sorted(thin))}.\n"
            "  По таким профилям строгие проверки намеренно смягчены, потому что по\n"
            "  одному-двум образцам нельзя отличить постоянную величину от совпадения.\n"
            "  Эти пропуски закроются сами, как только образцов станет больше."
        )
    hard = sum(by_attack[a]["missed"] for a in HARD_BY_DESIGN if a in by_attack)
    if hard:
        print(
            f"\n  {hard} пропусков — пересохран, не меняющий структуру файла. Структурный\n"
            "  анализ такое не берёт в принципе: файл остаётся неотличим от оригинала.\n"
            "  Против этого работает только сверка с реальным поступлением денег."
        )
    print(
        "\nПодделки смоделированы, а не собраны из живых образцов. Цифры показывают,\n"
        "что детектор ловит типовые способы порчи файла, и ничего не говорят о том,\n"
        "как он поведёт себя против подготовленного фальсификатора."
    )


if __name__ == "__main__":
    main()
