"""Снятие структурного «отпечатка» PDF.

Разбирается только служебная структура файла. Содержимое документа (суммы, имена,
номера счетов) здесь не читается — этим занимается отдельный модуль извлечения полей.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

RE_PRODUCER = re.compile(rb"/Producer\s*\(([^)]{0,120})\)")
RE_CREATOR = re.compile(rb"/Creator\s*\(([^)]{0,120})\)")
RE_OBJ = re.compile(rb"\b\d+\s+0\s+obj\b")
RE_FONT = re.compile(rb"/BaseFont\s*/([A-Za-z0-9+\-,._]+)")
RE_SUBTYPE = re.compile(rb"/Subtype\s*/(\w+)")

# ГОСТ-алгоритмы, которыми подписывают документы российские банки.
GOST_OIDS = {
    "1.2.643.7.1.1.1.1": "ГОСТ Р 34.10-2012 (256 бит)",
    "1.2.643.7.1.1.1.2": "ГОСТ Р 34.10-2012 (512 бит)",
    "1.2.643.7.1.1.2.2": "ГОСТ Р 34.11-2012 Стрибог-256",
    "1.2.643.7.1.1.2.3": "ГОСТ Р 34.11-2012 Стрибог-512",
    "1.2.643.2.2.19": "ГОСТ Р 34.10-2001",
    "1.2.643.2.2.9": "ГОСТ Р 34.11-94",
}

SIGNATURE_MARKERS = (b"/ByteRange", b"Adobe.PPKLite", b"adbe.pkcs7", b"ETSI.CAdES")


@dataclass(slots=True)
class SignatureInfo:
    """Что удалось узнать о встроенной электронной подписи."""

    present: bool = False
    count: int = 0
    field_names: list[str] = field(default_factory=list)
    digest_algorithm: str | None = None
    signature_algorithm: str | None = None
    is_gost: bool = False
    covers_whole_file: bool | None = None
    """False означает, что после подписи в файл что-то дописали."""

    parse_error: str | None = None


@dataclass(slots=True)
class Fingerprint:
    """Структурные признаки файла, по которым узнаётся шаблон банка."""

    path: str
    size: int
    pdf_version: str
    objects: int
    revisions: int
    """Число ревизий: 1 — файл не дописывался, 2 — одна дописка (обычно подпись)."""

    producer: str | None
    creator: str | None
    fonts: list[str]
    subtypes: list[str]
    has_image: bool
    encrypted: bool
    signature: SignatureInfo

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def template_key(self) -> tuple:
        """Признаки, которые обязаны совпадать у документов одного шаблона."""
        return (
            self.pdf_version,
            self.objects,
            self.producer,
            tuple(self.fonts),
            tuple(self.subtypes),
            self.has_image,
            self.signature.present,
        )


def _read_signature(path: Path, data: bytes) -> SignatureInfo:
    if not any(m in data for m in SIGNATURE_MARKERS):
        return SignatureInfo(present=False)

    info = SignatureInfo(present=True)
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader

        with path.open("rb") as fh:
            reader = PdfFileReader(fh)
            sigs = list(reader.embedded_signatures)
            info.count = len(sigs)
            for sig in sigs:
                info.field_names.append(sig.field_name)
                si = sig.signer_info
                digest = si["digest_algorithm"]["algorithm"].native
                sig_alg = si["signature_algorithm"]["algorithm"].native
                info.digest_algorithm = GOST_OIDS.get(digest, digest)
                info.signature_algorithm = GOST_OIDS.get(sig_alg, sig_alg)
                info.is_gost = str(digest).startswith("1.2.643") or str(
                    sig_alg
                ).startswith("1.2.643")
                # Покрывает ли подпись файл целиком, или после неё что-то дописали.
                info.covers_whole_file = sig.signed_data is not None and not (
                    sig.external_md_algorithm is None and False
                )
                try:
                    info.covers_whole_file = sig.coverage.name in {
                        "ENTIRE_FILE",
                        "ENTIRE_REVISION",
                    }
                except Exception:
                    info.covers_whole_file = None
    except Exception as exc:  # подпись есть, но разобрать не удалось
        info.parse_error = f"{type(exc).__name__}: {exc}"

    return info


def _inflated_streams(data: bytes) -> list[bytes]:
    """Содержимое Flate-потоков: шрифты Сбера лежат в ObjStm, не в сыром файле."""
    out: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        raw = match.group(1)
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith((b"\n", b"\r")):
            raw = raw[:-1]
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                out.append(zlib.decompress(raw, wbits))
                break
            except zlib.error:
                continue
    return out


def fingerprint(path: str | Path) -> Fingerprint:
    path = Path(path)
    data = path.read_bytes()
    payloads = [data, *_inflated_streams(data)]

    producer = RE_PRODUCER.search(data)
    creator = RE_CREATOR.search(data)
    fonts = sorted(
        {
            f.decode().split("+")[-1]
            for blob in payloads
            for f in RE_FONT.findall(blob)
        }
    )
    subtypes = sorted({s.decode() for blob in payloads for s in RE_SUBTYPE.findall(blob)})

    return Fingerprint(
        path=str(path),
        size=len(data),
        pdf_version=data[5:8].decode("latin-1", "replace"),
        objects=len(RE_OBJ.findall(data)),
        revisions=max(1, data.count(b"startxref")),
        producer=producer.group(1).decode("latin-1") if producer else None,
        creator=creator.group(1).decode("latin-1") if creator else None,
        fonts=fonts,
        subtypes=subtypes,
        has_image=b"/Image" in data,
        encrypted=b"/Encrypt" in data,
        signature=_read_signature(path, data),
    )
