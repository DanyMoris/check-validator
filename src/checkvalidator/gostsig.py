"""Проверка электронной подписи ГОСТ в PDF.

В справках ВТБ стоит усиленная подпись банка: ГОСТ Р 34.10-2012 и хеш Стрибог-256.
Проверка состоит из трёх независимых шагов:

1. Подпись покрывает весь файл, кроме самой подписи (ByteRange).
2. Хеш содержимого совпадает с тем, что запечатано в подписи (messageDigest).
3. Криптография сходится: подпись создана закрытым ключом из сертификата,
   а сертификат принадлежит известному банку (ИНН в белом списке).

Без шага 3 злоумышленник мог бы украсть сертификат ВТБ из настоящей справки
и подложить его в поддельный файл. С шагом 3 это не проходит: без закрытого
ключа банка подпись не сойдётся.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import gostcrypto
from pyhanko.pdf_utils.reader import PdfFileReader

# ИНН подписанта, которым мы доверяем. Это не секрет: ИНН банка публичен.
# Подпись принимается только если криптография сошлась И ИНН в этом списке.
TRUSTED_SIGNERS: dict[str, str] = {
    "7702070139": "Банк ВТБ (ПАО)",
}

CURVE_NAME = "id-tc26-gost-3410-2012-256-paramSetB"
# OID 1.2.643.2.2.36.0 (CryptoPro-A) совпадает с paramSetB ТК 26.
INN_OID = "1.2.643.100.4"


@dataclass(slots=True)
class GostVerifyResult:
    present: bool
    crypto_ok: bool
    digest_matches: bool
    covers_file: bool
    trusted_signer: bool
    signer_name: str | None = None
    signer_inn: str | None = None
    issuer_name: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Документ подписан доверенным банком и с тех пор не менялся."""
        return (
            self.present
            and self.crypto_ok
            and self.digest_matches
            and self.covers_file
            and self.trusted_signer
        )


def _streebog256(data: bytes) -> bytes:
    return bytes(gostcrypto.gosthash.new("streebog256", data=bytearray(data)).digest())


def _rev32(blob: bytes) -> bytes:
    return b"".join(blob[i : i + 32][::-1] for i in range(0, len(blob), 32))


def _public_key(cert) -> bytes:
    """64 байта X||Y в big-endian, как ждёт gostcrypto."""
    native: bytes = cert["tbs_certificate"]["subject_public_key_info"]["public_key"].native
    xy = native[-64:]
    return _rev32(xy)


def _signature_rs(raw: bytes) -> bytes:
    """CMS (RFC 4490) хранит s||r; gostcrypto ждёт r||s."""
    return raw[32:] + raw[:32]


def _signed_attrs_for_hash(signer_info) -> bytes:
    raw = signer_info["signed_attrs"].dump()
    return b"\x31" + raw[1:]


def _message_digest_attr(signer_info) -> bytes | None:
    for attr in signer_info["signed_attrs"]:
        if attr["type"].native == "message_digest":
            value = list(attr["values"])[0].native
            return bytes(value)
    return None


def _content_hash(data: bytes, byte_range) -> bytes:
    start1, len1, start2, len2 = (int(x) for x in byte_range)
    hasher = gostcrypto.gosthash.new("streebog256")
    hasher.update(bytearray(data[start1 : start1 + len1]))
    hasher.update(bytearray(data[start2 : start2 + len2]))
    return bytes(hasher.digest())


def _covers_file(data: bytes, byte_range) -> bool:
    start1, len1, start2, len2 = (int(x) for x in byte_range)
    if start1 != 0:
        return False
    if start1 + len1 > start2:
        return False
    if start2 + len2 != len(data):
        return False
    return True


def _name_cn(name) -> str | None:
    native = name.native
    if isinstance(native, dict):
        cn = native.get("common_name")
        if isinstance(cn, list):
            return cn[0] if cn else None
        return cn
    return None


def _name_inn(name) -> str | None:
    try:
        for rdn in name.chosen:
            for tv in rdn:
                dotted = tv["type"].dotted if hasattr(tv["type"], "dotted") else str(tv["type"].native)
                if dotted == INN_OID or tv["type"].native == INN_OID:
                    val = tv["value"].native
                    return str(val)
    except Exception:
        return None
    return None


def _verify_crypto(digest: bytes, signature: bytes, public_key: bytes) -> bool:
    curve = gostcrypto.gostsignature.CURVES_R_1323565_1_024_2019[CURVE_NAME]
    signer = gostcrypto.gostsignature.new(gostcrypto.gostsignature.MODE_256, curve)
    try:
        return bool(
            signer.verify(
                bytearray(public_key),
                bytearray(digest[::-1]),
                bytearray(signature),
            )
        )
    except Exception:
        return False


def verify_pdf_gost(path: str | Path) -> GostVerifyResult:
    path = Path(path)
    data = path.read_bytes()
    try:
        with path.open("rb") as fh:
            reader = PdfFileReader(fh)
            signatures = list(reader.embedded_signatures)
    except Exception as exc:
        return GostVerifyResult(
            present=False,
            crypto_ok=False,
            digest_matches=False,
            covers_file=False,
            trusted_signer=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    if not signatures:
        return GostVerifyResult(
            present=False,
            crypto_ok=False,
            digest_matches=False,
            covers_file=False,
            trusted_signer=False,
        )

    sig = signatures[0]
    si = sig.signer_info
    cert = list(sig.signed_data["certificates"])[0].chosen
    signer_name = _name_cn(cert.subject)
    signer_inn = _name_inn(cert.subject)
    issuer_name = _name_cn(cert.issuer)

    covers = _covers_file(data, sig.byte_range)
    md_attr = _message_digest_attr(si)
    content = _content_hash(data, sig.byte_range)
    digest_matches = md_attr is not None and md_attr == content

    attrs = _signed_attrs_for_hash(si)
    attr_digest = _streebog256(attrs)
    crypto_ok = _verify_crypto(
        attr_digest,
        _signature_rs(bytes(si["signature"].native)),
        _public_key(cert),
    )
    trusted = bool(signer_inn and signer_inn in TRUSTED_SIGNERS)

    return GostVerifyResult(
        present=True,
        crypto_ok=crypto_ok,
        digest_matches=digest_matches,
        covers_file=covers,
        trusted_signer=trusted,
        signer_name=signer_name,
        signer_inn=signer_inn,
        issuer_name=issuer_name,
    )
