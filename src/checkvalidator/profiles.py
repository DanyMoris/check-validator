"""Реестр эталонных профилей: как выглядит настоящий документ каждого банка.

Профиль — это данные, а не код. Он выводится из папки с образцами скриптом
tools/build_profiles.py, поэтому добавить новый банк или тип документа значит
положить файлы в resources/ и перезапустить сборку профилей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "profiles" / "profiles.yaml"

MIN_SAMPLES_ESTABLISHED = 3
"""Меньше трёх образцов — по ним нельзя отличить постоянную величину от совпадения."""


@dataclass(slots=True)
class Range:
    min: int
    max: int

    def contains(self, value: int, tolerance: int = 0) -> bool:
        return self.min - tolerance <= value <= self.max + tolerance

    def as_dict(self) -> dict[str, int]:
        return {"min": self.min, "max": self.max}


@dataclass(slots=True)
class Profile:
    """Эталон для одной пары «банк + тип документа»."""

    id: str
    bank: str
    doc_type: str
    samples: int
    pdf_versions: list[str]
    objects: Range
    producers: list[str | None]
    font_sets: list[list[str]]
    subtype_sets: list[list[str]]
    has_image: list[bool]
    signature_expected: bool
    revisions: Range
    size: Range
    notes: str = ""
    quirks: list[str] = field(default_factory=list)

    @property
    def established(self) -> bool:
        """Хватает ли образцов, чтобы доверять точным значениям профиля."""
        return self.samples >= MIN_SAMPLES_ESTABLISHED

    @property
    def confidence(self) -> str:
        return "established" if self.established else "provisional"

    @classmethod
    def from_dict(cls, profile_id: str, d: dict[str, Any]) -> Profile:
        return cls(
            id=profile_id,
            bank=d["bank"],
            doc_type=d["doc_type"],
            samples=int(d["samples"]),
            pdf_versions=list(d["pdf_versions"]),
            objects=Range(**d["objects"]),
            producers=list(d["producers"]),
            font_sets=[list(fs) for fs in d["font_sets"]],
            subtype_sets=[list(ss) for ss in d["subtype_sets"]],
            has_image=list(d["has_image"]),
            signature_expected=bool(d["signature_expected"]),
            revisions=Range(**d["revisions"]),
            size=Range(**d["size"]),
            notes=d.get("notes", ""),
            quirks=list(d.get("quirks", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bank": self.bank,
            "doc_type": self.doc_type,
            "samples": self.samples,
            "pdf_versions": self.pdf_versions,
            "objects": self.objects.as_dict(),
            "producers": self.producers,
            "font_sets": self.font_sets,
            "subtype_sets": self.subtype_sets,
            "has_image": self.has_image,
            "signature_expected": self.signature_expected,
            "revisions": self.revisions.as_dict(),
            "size": self.size.as_dict(),
            "notes": self.notes,
            "quirks": self.quirks,
        }


class ProfileRegistry:
    def __init__(self, profiles: dict[str, Profile]):
        self._profiles = profiles

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self):
        return iter(self._profiles.values())

    def get(self, profile_id: str) -> Profile | None:
        return self._profiles.get(profile_id)

    def ids(self) -> list[str]:
        return sorted(self._profiles)

    def match(self, fp) -> tuple[Profile | None, list[Profile]]:
        """Подбирает профиль по отпечатку.

        Возвращает (лучший профиль, все кандидаты). Опознание идёт по producer и
        набору шрифтов: это самые устойчивые признаки шаблона. Если совпадений нет,
        профиль неизвестен — это не повод считать документ поддельным.
        """
        fonts = sorted(fp.fonts)

        def producer_matches(p: Profile) -> bool:
            # Отсутствие producer ничего не опознаёт: пустое значение встречается
            # у самых разных файлов. Иначе любой PDF без этого поля прилипал бы к
            # профилю ВТБ/Справка, где банк его действительно не проставляет.
            return fp.producer is not None and fp.producer in p.producers

        def fonts_match(p: Profile) -> bool:
            return bool(fonts) and fonts in [sorted(f) for f in p.font_sets]

        candidates = [
            p for p in self._profiles.values() if producer_matches(p) or fonts_match(p)
        ]
        if not candidates:
            return None, []

        def score(p: Profile) -> tuple[int, int, int]:
            return (
                int(producer_matches(p)),
                int(fonts_match(p)),
                int(p.objects.contains(fp.objects)),
            )

        best = max(candidates, key=score)
        return best, candidates

    @classmethod
    def load(cls, path: str | Path | None = None) -> ProfileRegistry:
        path = Path(path) if path else DEFAULT_PATH
        if not path.exists():
            return cls({})
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = raw.get("profiles", {}) or {}
        return cls({k: Profile.from_dict(k, v) for k, v in entries.items()})

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else DEFAULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "# Профили собираются из resources/ скриптом tools/build_profiles.py": None,
            "profiles": {pid: p.to_dict() for pid, p in sorted(self._profiles.items())},
        }
        payload.pop("# Профили собираются из resources/ скриптом tools/build_profiles.py")
        path.write_text(
            "# Эталонные профили банковских документов.\n"
            "# Собирается автоматически: tools/build_profiles.py\n"
            "# Править вручную можно, но при пересборке правки потеряются —\n"
            "# кроме полей notes и quirks, они сохраняются.\n\n"
            + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        return path
