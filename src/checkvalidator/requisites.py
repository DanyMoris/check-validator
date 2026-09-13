"""Контрольные суммы российских реквизитов: ИНН и ключ расчётного счёта."""

from __future__ import annotations

_INN10_W = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_ACC_W = (7, 1, 3) * 8  # 24 веса, из них берутся 23 символа


def inn_checksum_ok(inn: str) -> bool:
    """Проверяет контрольные цифры ИНН (10 или 12 знаков)."""
    if not inn.isdigit():
        return False
    if len(inn) == 10:
        total = sum(int(inn[i]) * _INN10_W[i] for i in range(9))
        return (total % 11) % 10 == int(inn[9])
    if len(inn) == 12:
        n11 = (sum(int(inn[i]) * _INN12_W1[i] for i in range(10)) % 11) % 10
        n12 = (sum(int(inn[i]) * _INN12_W2[i] for i in range(11)) % 11) % 10
        return n11 == int(inn[10]) and n12 == int(inn[11])
    return False


def _account_mod10(prefix: str, account: str) -> int:
    code = prefix + account
    return sum(int(a) * b for a, b in zip(code, _ACC_W)) % 10


def account_key_ok(bik: str, account: str) -> bool:
    """Ключ 9-го разряда счёта по правилам ЦБ.

    Для счёта в кредитной организации берётся хвост БИК, для счёта в РКЦ —
    «0» и разряды 5–6 БИК. Настоящий документ проходит хотя бы один вариант;
    оба провала означают, что реквизиты собраны с ошибкой или выдуманы.
    """
    if not (bik.isdigit() and account.isdigit() and len(bik) == 9 and len(account) == 20):
        return False
    return _account_mod10(bik[-3:], account) == 0 or _account_mod10("0" + bik[4:6], account) == 0
