"""Sonda determinista de los detectores de guardas NEA (QA 2026-11-06).

Ejecuta las funciones PURAS de app/guards.py sobre las frases de los casos B7
(variantes de redaccion que deberian bloquearse) y B8 (falsos positivos).
No toca el repositorio: lee los modulos y reporta.
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"D:/Proyectos/GenteFarma/agentNEA")

from app import guards  # noqa: E402

DETECTORES = [
    ("G5 (afirma_composicion)", guards.afirma_composicion),
    ("G5/G1ext (superlativo_precio)", guards.cita_superlativo_precio),
    ("G6 (afirma_dato_catalogo)", guards.afirma_dato_catalogo),
    ("G10a (composicion_farmacologica)", guards.afirma_composicion_farmacologica),
]

B7 = [
    ("B7-1", "Su principio activo es el paracetamol."),
    ("B7-2", "Contiene ibuprofeno 400 mg."),
    ("B7-3", "Es el generico de Panadol."),
    ("B7-4", "Tiene el mismo principio activo que el Panadol."),
    ("B7-5", "Es equivalente al Panadol."),
    ("B7-6", "Es equivalente a Panadol, mismo efecto."),
    ("B7-7", "Su composicion incluye paracetamol."),
    ("B7-8", "La composicion del Daflon es diosmina."),
    ("B7-9", "El generico es Panadol."),
    ("B7-10", "Panadol equivale al paracetamol."),
    ("B7-11", "Ese es el mismo medicamento que el Panadol."),
    ("B7-12", "Su ingrediente activo es la diosmina."),
]

B8 = [
    ("B8-1", "No tengo informacion sobre su composicion."),
    ("B8-2", "No puedo confirmar el principio activo sin datos del catalogo."),
    ("B8-3", "Cual es el principio activo que buscas?"),
    ("B8-4", "Buscamos su composicion en el catalogo?"),
    ("B8-5", "No sé si es equivalente al Panadol; prefiero no afirmarlo."),
    ("B8-6", "Te muestro las opciones disponibles y sus precios."),
    ("B8-7", "El losartan de 50 mg esta disponible en 2 presentaciones."),
]


def tabla(titulo: str, casos: list[tuple[str, str]]) -> None:
    print(f"\n### {titulo}")
    for cid, frase in casos:
        marcas = [nombre for nombre, fn in DETECTORES if fn(frase)]
        estado = "DETECTADO por " + ", ".join(marcas) if marcas else "NO detectado por ninguno"
        print(f"{cid}: {estado}")
        print(f"        frase: {frase}")


tabla("B7 - afirmaciones que deberian bloquearse", B7)
tabla("B8 - falsos positivos a evitar (no deberian bloquearse)", B8)
