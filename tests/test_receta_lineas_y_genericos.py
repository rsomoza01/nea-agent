"""Ruido de búsqueda en una receta escrita (WhatsApp, 2026-11-24 18:12).

El cliente mandó 11 medicamentos y el agente cotizó cosas que no había pedido:

```
VIt e.                     → título «VIT» con VIT C, VIT K, complejo B y HIDRAMER loción
Protectores securezza      → CEPILLO DENTAL «c/ tapa protectora» y BEDUCIS «dermo protectora»
Magnesio en polvo y
  efervecente              → DOS grupos: «MAGNESIO POLVO» y «EFERVECENTE» (con REDOZINC)
```

Tres causas independientes, todas en la extracción de términos:

1. `_es_letra_de_vitamina` solo entendía «vitamina», no la abreviatura «vit»: la letra se
   descartaba por corta y el término quedaba en `vit` (cualquier VIT del catálogo).
2. «protectores» es un descriptor, no un producto: entra como categoría y el término se
   queda en la marca (`securezza`).
3. `_lineas_lista_medicamentos` partía CUALQUIER texto por comas y por « y » (una sola
   coma en la lista desactivaba el respeto por las líneas) y separaba un producto que
   lleva « y » dentro de su nombre.
"""
from __future__ import annotations

from app.turn import (
    _es_continuacion_de_presentacion,
    _extraer_termino_medicamento,
    _lineas_lista_medicamentos,
)

RECETA = """Hola muy buenas tardes, por favor me puedes ayudar con estos medicamentos.
Flextrin,
Curcumix
VIt e.
Gluconato de calcio
Candesatan 16 MG x 30 Tab. la sante
Ácido fólico de 10 MG x 30 calox o gv
Tiras reactivas flemd
VIt d3 + k2
Magnesio 
Amlodipina de 5 MG marca calox o tv
Magnesio en polvo y efervecente
Protectores securezza"""


# --------------------------------------------------------------------------
# 1. La letra de la vitamina sobrevive a la abreviatura
# --------------------------------------------------------------------------
def test_vit_abreviado_conserva_la_letra() -> None:
    assert _extraer_termino_medicamento("VIt e.") == "vit e"
    assert _extraer_termino_medicamento("Vit e") == "vit e"
    assert _extraer_termino_medicamento("VIt d3 + k2") == "vit d3 k2"


def test_vitamina_escrita_entera_sigue_igual() -> None:
    assert _extraer_termino_medicamento("vitamina c") == "vitamina c"


# --------------------------------------------------------------------------
# 2. «protectores» no es un producto
# --------------------------------------------------------------------------
def test_el_descriptor_protectores_sale_del_termino() -> None:
    assert _extraer_termino_medicamento("Protectores securezza") == "securezza"


def test_protector_solo_no_es_consulta() -> None:
    assert _extraer_termino_medicamento("protectores") is None


def test_protector_solar_conserva_lo_que_identifica() -> None:
    assert _extraer_termino_medicamento("protector solar") == "solar"


# --------------------------------------------------------------------------
# 3. Las líneas de la receta se respetan
# --------------------------------------------------------------------------
def test_lista_de_receta_con_una_coma_no_se_desarma() -> None:
    lineas = _lineas_lista_medicamentos(RECETA)
    assert "Flextrin" in lineas
    assert "Curcumix" in lineas
    assert "VIt e." in lineas
    assert "Tiras reactivas flemd" in lineas
    # El producto que lleva « y » dentro de su nombre queda ENTERO y en una sola pieza.
    assert "Magnesio en polvo y efervecente" in lineas
    assert not any("\n" in l for l in lineas)


def test_el_conector_y_sigue_partiendo_dos_medicamentos() -> None:
    assert _lineas_lista_medicamentos("esoz, leprit y evigax") == ["esoz", "leprit", "evigax"]
    assert _lineas_lista_medicamentos("losartan y metformina") == ["losartan", "metformina"]


def test_la_cola_de_presentacion_no_parte_el_producto() -> None:
    assert _es_continuacion_de_presentacion("efervescente")
    assert _es_continuacion_de_presentacion("efervecente")   # typo del habla
    assert _es_continuacion_de_presentacion("tabletas recubiertas")
    assert not _es_continuacion_de_presentacion("metformina")
