"""Базовые типы: сигналы проверок и итоговый вердикт."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extract import ExtractedFields
    from .ledger import MatchResult


class Severity(str, Enum):
    """Насколько серьёзно сработавшее замечание."""

    INFO = "info"
    """Наблюдение. На вердикт не влияет."""

    WARN = "warn"
    """Подозрительно, но по одному такому признаку выводов не делаем."""

    CRITICAL = "critical"
    """Достаточно одного, чтобы признать документ поддельным."""


class Verdict(str, Enum):
    CONFIRMED = "ПОДТВЕРЖДЁН"
    """Подпись проверена либо найдено реальное поступление денег."""

    FORGED = "ПОДДЕЛКА"
    """Сработал хотя бы один критический признак."""

    UNCONFIRMED = "НЕ ПОДТВЕРЖДЁН"
    """Признаков подделки нет, но и подлинность не доказана."""


@dataclass(frozen=True, slots=True)
class Signal:
    """Результат одной проверки."""

    id: str
    passed: bool
    severity: Severity
    explanation: str
    """Формулировка для человека — попадает в ответ пользователю."""

    evidence: dict = field(default_factory=dict)
    """Что именно увидели: ожидаемое и фактическое значения."""

    @property
    def failed(self) -> bool:
        return not self.passed


@dataclass(slots=True)
class Report:
    """Итог проверки одного документа."""

    verdict: Verdict
    signals: list[Signal]
    profile_id: str | None = None
    summary: str = ""
    fields: ExtractedFields | None = None
    match: MatchResult | None = None

    @property
    def failures(self) -> list[Signal]:
        return [s for s in self.signals if s.failed]

    @property
    def critical_failures(self) -> list[Signal]:
        return [s for s in self.failures if s.severity is Severity.CRITICAL]

    @property
    def warnings(self) -> list[Signal]:
        return [s for s in self.failures if s.severity is Severity.WARN]
