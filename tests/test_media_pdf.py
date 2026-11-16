"""Regresión de la extracción de texto de PDF (`app.media._pdf_text`).

Qué fija esta prueba (contrato que consume `app.media.describe_item`):
- PDF válido con texto → devuelve el texto.
- Tope de páginas: `PDF_MAX_PAGES` manda; el texto de la página 11 no entra.
- PDF válido SIN texto → `""` (el llamador lo interpreta como «no pude leer» y
  manda el marcador honesto al cliente).
- PDF corrupto / cifrado → la excepción de pypdf se propaga AQUÍ; el turno no
  se rompe porque `describe_item` envuelve todo en `try/except`.

Los PDFs se generan en el test (bytes crudos), sin fixtures binarios en el repo.
"""
from __future__ import annotations

import io

import pytest

from app.media import PDF_MAX_PAGES, _pdf_text

pytest.importorskip("pypdf")


def _pdf_bytes(pages_text: list[str]) -> bytes:
    """PDF mínimo válido, una página por elemento, con texto en Helvetica."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)  # 1-based

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    content_ids: list[int] = []
    pages_node_id = 0  # se rellena abajo
    page_ids_placeholder: list[int] = []
    for text in pages_text:
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 40 760 Td ({esc}) Tj ET".encode()
        content_ids.append(
            add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        )
        page_ids_placeholder.append(0)

    pages_node_id = len(objs) + len(page_ids_placeholder) + 1
    page_ids: list[int] = []
    for content_id in content_ids:
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_node_id} 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    real_pages = add(f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode())
    assert real_pages == pages_node_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_node_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objs) + 1, catalog, xref)
    )
    return bytes(out)


RECETA = (
    "RECETA MEDICA - prueba QA. Amoxicilina 500 mg: 1 tableta cada 8 horas "
    "por 7 dias. Sin datos reales de paciente."
)


def test_extrae_texto_de_pdf_valido():
    texto = _pdf_text(_pdf_bytes([RECETA]))
    assert isinstance(texto, str)
    assert "Amoxicilina 500 mg" in texto
    assert "cada 8 horas" in texto


def test_respeta_el_tope_de_paginas():
    paginas = [f"Documento QA pagina {i} - Losartan 50 mg" for i in range(1, PDF_MAX_PAGES + 3)]
    texto = _pdf_text(_pdf_bytes(paginas))
    assert f"pagina {PDF_MAX_PAGES}" in texto
    assert f"pagina {PDF_MAX_PAGES + 1}" not in texto
    assert texto.count("Documento QA") == PDF_MAX_PAGES


def test_pdf_valido_sin_texto_devuelve_cadena_vacia():
    # El llamador (describe_item) convierte "" en el marcador honesto al cliente.
    assert _pdf_text(_pdf_bytes([""])) == ""


def test_pdf_truncado_propaga_la_excepcion():
    data = _pdf_bytes([RECETA])
    with pytest.raises(Exception):
        _pdf_text(data[: len(data) // 2])


def test_pdf_basura_propaga_la_excepcion():
    with pytest.raises(Exception):
        _pdf_text(b"%PDF-1.4\n" + b"\x00\xff\x1a\n" * 40)


def test_pdf_cifrado_propaga_la_excepcion():
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=io.BytesIO(_pdf_bytes([RECETA])))
    writer.encrypt("clave-qa")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(Exception):
        _pdf_text(buf.getvalue())
