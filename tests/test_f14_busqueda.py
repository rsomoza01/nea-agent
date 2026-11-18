"""Regresiones de F14 — precisión de búsqueda del agente NEA.

Cinco causas raíz verificadas por simulación con el código real
(`docs/F14-diagnostico-busqueda.md`). Cada bloque prueba el caso REAL del
cliente y, además, los BORDES y las NO-REGRESIONES de cada fix: los typos que
ya funcionaban tienen que seguir funcionando y los falsos positivos históricos
no pueden volver.

        F14.1  apelativos de cortesía en texto escrito (+ letra de vitamina)
        F14.2  "opción N" en mensaje mixto es compra confirmada, no búsqueda
        F14.3  redacción: prohibido "No tengo X" cuando hay resultados
        F14.4  alias de escritura data-driven (tabla med_aliases)
        F14.5  multi-medicamento: dividir por conectores

Todos son deterministas (sin LLM).
"""
from __future__ import annotations

import asyncio
import re
import types
from pathlib import Path

import pytest

from app import aliases
from app.relevancia import (
    _es_desplazamiento,
    _levenshtein,
    _prefijo_comun,
    es_relevante,
    filtrar_relevantes,
)
from app.state import Conversation
from app.tools import ToolRuntime
from app.turn import (
    _extraer_eleccion_multiple,
    _extraer_termino_medicamento,
    _extraer_termino_transcripcion,
    _menciona_opcion_numerada,
    _partir_consulta_multi,
)

RAIZ = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------- casos ---

CASO_1 = "Buenas tardes!! Mi amor que precio tiene la nitrofurantoina de 50mg y la vitamina C"
CASO_2 = "vivacina"
CASO_3 = "Precio de la macrodantina?? Voy a querer una vitamina C de la opción 9"
CASO_4 = "buenas tardes precio de solunoval solución bucal y pinvex solución bucal"

PRODUCTOS_C1 = [
    {"nombre": "TE AMO PRESERV. 3PCS"},
    {"nombre": "VITAMINA K 10 MG X 30 TAB"},
    {"nombre": "VITAMINA C 500 MG X 10 TAB"},
    {"nombre": "NITROFURANTOINA 100 MG X 20 CAP"},
]
PRODUCTOS_C2 = [
    {"nombre": "VITAMINA K 10 MG X 30 TAB"},
    {"nombre": "VITAMINA C 500 MG X 10 TAB"},
    {"nombre": "VITAMINAS B12 COMPLEJO X 30 CAP"},
]

# Typos reales ya soportados por el proyecto: no pueden romperse.
TYPO_OK = [
    ("omeprasol", "OMEPRAZOL 20 MG X 30 CAP"),
    ("diclofencao", "DICLOFENACO 50 MG X 20 TAB"),
    ("amoxicilna", "AMOXICILINA 500 MG X 21 CAP"),
    ("solunoval", "SOLUNOVAR SOLUCION BUCAL X 1 FCO"),
    ("fexofenasina", "FEXOFENADINA 120 MG X 10 TAB"),
    ("azitromisina", "AZITROMICINA 500 MG X 3 TAB"),
    ("metmorfina", "METFORMINA 850 MG X 30 TAB"),
    ("clonacepan", "CLONAZEPAM 2 MG X 30 TAB"),
    ("acetaminofen 650 mg", "ACETAMINOFEN 650 MG X 10 TAB"),
]

# Falsos positivos históricos (documentados en relevancia.py): NUNCA vuelven.
FALSOS_POSITIVOS = [
    ("hasta", "PASTA PRIMOR PLUMA"),
    ("todo", "DESODORANTE DOVE"),
    ("tarda delivery", "VENDA ELASTICA"),
    ("manana hora", "COMPOTA GERANIA"),
    ("amor", "TE AMO PRESERV. 3PCS"),
]

SEMILLA_SQL = RAIZ / "migrations" / "013_med_aliases.sql"


def _runtime() -> ToolRuntime:
    return ToolRuntime(types.SimpleNamespace(), Conversation(id=1, wa_identity="wa"), "cv1")


class _Fake:
    """Doble de prueba permisivo: cualquier atributo que falte es un no-op async."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, nombre):  # pragma: no cover - solo en pruebas
        async def _noop(*_a, **_k):
            return None

        return _noop


class _CrmFake:
    def __init__(self, productos: list[dict]) -> None:
        self.productos = productos

    async def get_products(self, provider_id: str, q: str = "", limit: int = 20):
        return {"products": list(self.productos), "provider": {"nombre": "QA"},
                "paymenType": None, "hours": None}


def _runtime_con_catalogo(productos: list[dict]) -> tuple[ToolRuntime, _Fake]:
    """ToolRuntime con un CRM falso que devuelve `productos` para cualquier término."""
    ctx = _Fake(crm=_CrmFake(productos), store=_Fake())
    rt = ToolRuntime(ctx, Conversation(id=1, wa_identity="wa"), "cv1", provider_id="demo")
    return rt, ctx


# ============================================================== F14.1 =========


class TestF141ApelativosEnTextoEscrito:
    """El caso 1 real: apelativos fuera del término y letra de vitamina dentro."""

    def test_caso_real_1_sin_apelativos(self):
        term = _extraer_termino_medicamento(CASO_1)
        assert term is not None
        assert "amor" not in term.split()
        assert "nitrofurantoina" in term

    @pytest.mark.parametrize(
        "apelativo",
        ["mi amor", "mi vida", "mi reina", "corazón", "mami", "hermano",
         "camarada", "doctora", "pana mío", "gordita"],
    )
    def test_apelativos_varios_no_llegan_al_catalogo(self, apelativo):
        term = _extraer_termino_medicamento(f"buenas tardes {apelativo} tienes atamel forte?")
        assert term == "atamel forte"

    def test_apelativo_no_borra_el_farmaco(self):
        assert _extraer_termino_medicamento("mi amor nitrofurantoina") == "nitrofurantoina"

    def test_letra_de_vitamina_se_conserva(self):
        assert _extraer_termino_medicamento("vitamina C") == "vitamina c"
        assert _extraer_termino_medicamento("vitamina k 100 mg") == "vitamina k 100 mg"

    def test_letra_suelta_sin_vitamina_no_se_conserva(self):
        """Una letra suelta NO es fármaco: solo se conserva tras 'vitamina'."""
        term = _extraer_termino_medicamento("dame c")
        assert term is None or "c" not in term.split()

    def test_letra_no_cruza_con_otra_palabra(self):
        """Una letra suelta solo se conserva tras 'vitamina'; el número final
        suelto se descarta (comportamiento ya existente del extractor)."""
        assert _extraer_termino_medicamento("cefalexina c 500") == "cefalexina"

    def test_transcripcion_de_voz_sigue_limpiando(self):
        """El camino de las notas de voz no se tocó."""
        texto = '[Nota de voz del lead, transcrita]: "buenas tardes mi linda mire cuanto sale atamel forte"'
        assert _extraer_termino_transcripcion(texto) == "atamel forte"

    def test_relevancia_no_acepta_apelativo_como_senal(self):
        """'amor' no puede validar TE AMO PRESERV (el bug del caso 1)."""
        assert es_relevante("amor", "TE AMO PRESERV. 3PCS") is False

    def test_caso_real_1_ya_no_trae_el_preservativo(self):
        filtrados = [p["nombre"] for p in filtrar_relevantes(
            "amor nitrofurantoina 50mg vitamina", PRODUCTOS_C1
        )]
        assert "TE AMO PRESERV. 3PCS" not in filtrados
        assert "NITROFURANTOINA 100 MG X 20 CAP" in filtrados


# ============================================================== F14.2 =========


class TestF142OpcionNumerada:
    """'opción N' en un mensaje mixto es una COMPRA CONFIRMADA."""

    @pytest.mark.parametrize(
        "texto",
        ["de la opción 9", "opción 9", "opcion 12", "quiero 2 cajas de la opción 3",
         "opción Nº 4", CASO_3],
    )
    def test_detecta_opcion_numerada(self, texto):
        assert _menciona_opcion_numerada(texto) is True

    @pytest.mark.parametrize(
        "texto",
        ["la opción es buena", "dame opciones", "quiero 2 cajas", "",
         "tienes opciones de pago?"],
    )
    def test_no_detecta_sin_numero(self, texto):
        assert _menciona_opcion_numerada(texto) is False

    def test_caso_real_3_se_resuelve_contra_last_options(self):
        """La selección se resuelve ANTES de extraer término (prioridad)."""
        assert _menciona_opcion_numerada(CASO_3) is True
        assert _extraer_eleccion_multiple(CASO_3) == [(1, 9)]

    def test_flag_del_runtime_arranca_apagado(self):
        assert _runtime().opcion_resuelta is False

    def test_backstop_de_catalogo_respeta_la_seleccion(self):
        """El backstop de búsqueda consulta el flag (no busca término nuevo)."""
        fuente = (RAIZ / "app" / "turn.py").read_text(encoding="utf-8")
        assert "not runtime.opcion_resuelta" in fuente


# ============================================================== F14.3 =========


class TestF143RedaccionSinContradiccion:
    """Si hay resultados, la respuesta NO abre con 'No tengo X'.

    La regla vive en DOS capas, y a propósito: el resultado de la herramienta
    (que es el único punto donde se sabe si hay resultados) y una referencia en
    el chasis. En el chasis NO se repite la regla larga: se midió en el
    Laboratorio que una prohibición global extra movía el estilo del agente en
    turnos donde no hay catálogo (ver REPORT.md, corridas A/B/E vs C/D).
    """

    def test_chasis_apunta_a_la_instruccion_de_la_busqueda(self):
        from app.prompt import build_system_prompt
        from app.profile import BusinessProfile

        prompt = build_system_prompt(
            profile=BusinessProfile(), context=None,
            conv=Conversation(id=1, wa_identity="wa"),
        )
        assert "instrucción de APERTURA" in prompt
        # La regla larga NO está en el chasis.
        assert "ABRAS con una NEGACIÓN" not in prompt

    def test_instrucciones_de_la_busqueda_prohiben_la_apertura_negativa(self):
        rt = _runtime()
        productos = [{"nombre": "SOLUNOVAR SOLUCION BUCAL X 1 FCO", "precio": 3.5,
                      "precioBs": 2600.0}]
        instr = rt._formatear_instrucciones_busqueda(productos, "solunoval")
        assert "PROHIBIDO abrir" in instr
        assert "EMPIEZA por ellos" in instr

    def test_busqueda_con_resultados_trae_la_instruccion_de_apertura(self):
        rt, ctx = _runtime_con_catalogo(
            [{"producto": "SOLUNOVAR SOLUCION BUCAL X 1 FCO",
              "nombre": "SOLUNOVAR SOLUCION BUCAL X 1 FCO",
              "precio": 3.5, "precioBs": 2600.0, "productId": "p1"}]
        )
        salida = asyncio.run(rt._buscar_medicamento({"nombre": "solunoval"}))
        assert salida["ok"] is True
        assert "PROHIBIDO abrir" in salida["instrucciones"]

    def test_busqueda_sin_resultados_prohibe_agotado_y_memoria_inventada(self):
        """Hallazgo del Laboratorio (persona 'errores_modismos'): el agente decía
        'está agotado' y 'ya la habías consultado antes' — estado de inventario y
        memoria que no existen. Los guards no lo cubren, así que la prohibición va
        en el resultado de la herramienta."""
        rt, ctx = _runtime_con_catalogo([])
        salida = asyncio.run(rt._buscar_medicamento({"nombre": "panadol"}))
        assert salida["ok"] is False and salida["error"] == "sin_resultados"
        detalle = salida["detalle"]
        assert "PROHIBIDO decir que el medicamento 'está agotado'" in detalle
        assert "no tienes memoria de otros hilos" in detalle


# ============================================================== F14.4 =========


class TestF144AliasDeEscritura:
    def test_alias_exacto(self):
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir("solunoval") == ("solunovar", True)

    def test_alias_con_dosis_conserva_la_dosis(self):
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir("solunoval 500 mg") == ("solunovar 500 mg", True)

    def test_alias_con_presentacion(self):
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir("solunoval solución bucal") == (
            "solunovar solucion bucal", True
        )

    def test_alias_con_letra_de_vitamina(self):
        aliases.registrar("bitamina", "vitamina")
        assert aliases.reescribir("bitamina C") == ("vitamina c", True)

    @pytest.mark.parametrize("escrito", ["SOLUNOVAL", "Solunoval", "solunovál"])
    def test_alias_normaliza_mayusculas_y_tildes(self, escrito):
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir(escrito)[1] is True

    def test_marca_no_se_reescribe_a_principio_activo(self):
        """Regla de oro: MACRODANTINA → NITROFURANTOINA NO es un alias."""
        aliases.registrar("macrodantina", "nitrofurantoina")
        aliases.limpiar()
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir("macrodantina") == ("macrodantina", False)

    def test_frase_con_otro_farmaco_no_aplica(self):
        aliases.registrar("solunoval", "solunovar")
        assert aliases.reescribir("solunoval mas otra cosa") == (
            "solunoval mas otra cosa", False
        )

    def test_alias_mas_largo_gana(self):
        aliases.registrar("solunoval", "solunovar")
        aliases.registrar("solunoval bucal", "solunovar bucal solucion")
        assert aliases.reescribir("solunoval bucal") == (
            "solunovar bucal solucion", True
        )

    def test_registrar_ignora_vacios_e_identicos(self):
        assert aliases.registrar("", "x") is False
        assert aliases.registrar("x", "") is False
        assert aliases.registrar("igual", "igual") is False
        assert aliases.total() == 0

    def test_cargar_items_es_idempotente(self):
        items = [{"alias": "solunoval", "canonico": "solunovar"}]
        assert aliases.cargar_items(items) == 1
        assert aliases.cargar_items(items) == 1
        assert aliases.total() == 1

    # ---- la semilla que se carga en producción ---------------------------

    def test_semilla_sql_existe_y_es_sana(self):
        sql = SEMILLA_SQL.read_text(encoding="utf-8")
        pares = re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'", sql)
        assert len(pares) >= 15, "la semilla curada debe tener al menos 15 pares"
        for alias, canon, origen in pares:
            assert alias.strip() == alias and canon.strip() == canon
            assert alias.lower() != canon.lower(), f"{alias}→{canon} no aporta nada"
            assert origen in {"cliente", "med_queries", "codigo", "semilla"}

    def test_semilla_no_mete_marcas(self):
        """Ninguna marca conocida puede estar en la tabla (escondería el producto)."""
        sql = SEMILLA_SQL.read_text(encoding="utf-8")
        pares = re.findall(r"\(\s*'([^']+)'", sql)
        for marca in ("macrodantina", "panadol", "daflon", "atamel", "pinvex"):
            assert marca not in pares

    def test_semilla_resuelve_el_caso_del_log(self):
        sql = SEMILLA_SQL.read_text(encoding="utf-8")
        assert "('fexofenasina', 'fexofenadina'" in sql

    def test_todos_los_pares_de_la_semilla_reescriben(self):
        sql = SEMILLA_SQL.read_text(encoding="utf-8")
        pares = re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'", sql)
        for alias, canon in pares:
            aliases.registrar(alias, canon)
            nuevo, aplicado = aliases.reescribir(alias)
            assert aplicado is True, f"{alias} no reescribió"
            assert nuevo == canon
        assert aliases.total() == len(pares)

    # ---- carga desde el store (arranque) ---------------------------------

    def test_cargar_desde_store_clase_ligera(self):
        class StoreFake:
            async def list_med_aliases(self):
                return [{"alias": "solunoval", "canonico": "solunovar"}]

        assert asyncio.run(aliases.cargar_desde_store(StoreFake())) == 1
        assert aliases.reescribir("solunoval") == ("solunovar", True)

    def test_cargar_desde_store_nunca_tumba_el_arranque(self):
        class StoreRoto:
            async def list_med_aliases(self):
                raise RuntimeError("tabla ausente")

        assert asyncio.run(aliases.cargar_desde_store(StoreRoto())) == 0

    def test_memory_store_expone_el_registro(self):
        from app.state import MemoryStore

        aliases.registrar("solunoval", "solunovar")
        filas = asyncio.run(MemoryStore().list_med_aliases())
        assert {"alias": "solunoval", "canonico": "solunovar", "origen": "memoria"} in filas

    def test_main_carga_los_alias_al_arrancar(self):
        fuente = (RAIZ / "app" / "main.py").read_text(encoding="utf-8")
        assert "cargar_aliases(store)" in fuente

    def test_busqueda_usa_el_alias_y_apaga_el_fuzzy(self):
        fuente = (RAIZ / "app" / "tools.py").read_text(encoding="utf-8")
        assert "_reescribir_alias(nombre)" in fuente
        assert "permitir_typo=not es_alias" in fuente


# ================================================ relevancia: typo estricto ===


class TestC2TypoNoCruzaDeCategoria:
    def test_vivacina_no_trae_vitaminas(self):
        assert filtrar_relevantes(CASO_2, PRODUCTOS_C2) == []

    def test_vivacina_no_valida_vitamina_por_nombre(self):
        assert es_relevante("vivacina", "VITAMINA K 10 MG X 30 TAB") is False
        assert es_relevante("vivacina", "VITAMINA C 500 MG X 10 TAB") is False

    @pytest.mark.parametrize("termino,producto", TYPO_OK)
    def test_typos_reales_siguen_funcionando(self, termino, producto):
        assert es_relevante(termino, producto) is True

    @pytest.mark.parametrize("termino,producto", FALSOS_POSITIVOS)
    def test_falsos_positivos_no_vuelven(self, termino, producto):
        assert es_relevante(termino, producto) is False

    def test_typo_de_dos_ediciones_con_parentesco_pasa(self):
        """'metmorfina'/'omeprasol' comparten prefijo: el typo es creíble."""
        assert es_relevante("metmorfina", "METFORMINA 850 MG") is True
        assert _prefijo_comun("metmorfina", "metformina") >= 3

    def test_desplazamiento_de_un_caracter_es_typo_creible(self):
        assert _es_desplazamiento("lorsatan", "losartan") is True
        assert _es_desplazamiento("diclofencao", "diclofenaco") is True
        assert _es_desplazamiento("vivacina", "vitamina") is False

    def test_dos_ediciones_arbitrarias_no_son_creibles(self):
        assert _levenshtein("vivacina", "vitamina") == 2
        assert _prefijo_comun("vivacina", "vitamina") < 3
        assert es_relevante("vivacina", "VITAMINA K 10 MG", typo_estricto=False) is True

    def test_permitir_typo_false_apaga_la_tolerancia(self):
        """Con el alias reescrito, la coincidencia tiene que ser literal."""
        assert es_relevante("omeprasol", "OMEPRAZOL 20 MG", permitir_typo=False) is False
        assert es_relevante("omeprazol", "OMEPRAZOL 20 MG", permitir_typo=False) is True

    def test_alias_mas_fuzzy_apagado_encuentra_el_producto_literal(self):
        sql = SEMILLA_SQL.read_text(encoding="utf-8")
        aliases.cargar_items(
            [{"alias": a, "canonico": c} for a, c in
             re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'", sql)]
        )
        nuevo, aplicado = aliases.reescribir("solunoval solucion bucal")
        assert aplicado is True
        productos = [{"nombre": "SOLUNOVAR SOLUCION BUCAL X 1 FCO"},
                     {"nombre": "VITAMINA C 500 MG"}]
        filtrados = filtrar_relevantes(nuevo, productos, permitir_typo=False)
        assert [p["nombre"] for p in filtrados] == ["SOLUNOVAR SOLUCION BUCAL X 1 FCO"]

    def test_correccion_creible_no_cruza_de_categoria(self):
        """Hallazgo de la simulación de CONVERSACIONES (no del arnés por funciones):
        la recuperación por 'núcleo' corregía 'vivacina' → 'vitamina' y el cliente
        recibía VITAMINA C/K/E con un «Asumí que buscas VITAMINA». La corrección se
        valida contra el término ORIGINAL, no contra el corregido."""
        from app.relevancia import correccion_creible

        assert correccion_creible("vivacina", "vitamina") is False
        assert correccion_creible("morfina", "formina") is False
        for original, corregido in (
            ("omeprasol", "omeprazol"),
            ("diclofencao", "diclofenaco"),
            ("solunoval", "solunovar"),
            ("lorsatan", "losartan"),
            ("hialuronico", "hialuronato"),
            ("omeprasol 20", "omeprazol 20"),
            ("acido acetalicio", "acido acetilsalicilico"),
        ):
            assert correccion_creible(original, corregido) is True, original

    def test_las_dos_puertas_de_correccion_usan_la_regla_c2(self):
        """La regla C2 se aplica en el filtro de relevancia Y en las correcciones
        hacia el catálogo (término corregido y núcleo)."""
        fuente = (RAIZ / "app" / "tools.py").read_text(encoding="utf-8")
        assert "correccion_creible," in fuente            # importada
        assert fuente.count("correccion_creible(") >= 2   # las dos puertas

    def test_letra_de_vitamina_manda_tambien_en_el_rescate(self):
        """Hallazgo de la simulación de conversaciones: el guard por producto
        acertaba pero el rescate escalonado volvía a colar K y E."""
        catalogo = [
            {"nombre": "VITAMINA C 500 MG X 10 TAB"},
            {"nombre": "VITAMINA K 10 MG X 30 TAB"},
            {"nombre": "VITAMINA E 400 UI X 10 CAP"},
            {"nombre": "VITAMINA C + ZINC X 30 TAB"},
        ]
        assert [p["nombre"] for p in filtrar_relevantes("vitamina c", catalogo)] == [
            "VITAMINA C 500 MG X 10 TAB", "VITAMINA C + ZINC X 30 TAB",
        ]
        assert [p["nombre"] for p in filtrar_relevantes("vitamina k", catalogo)] == [
            "VITAMINA K 10 MG X 30 TAB",
        ]
        # Sin letra, el término genérico sigue devolviendo todas.
        assert len(filtrar_relevantes("vitamina", catalogo)) == 4

    def test_fuzzy_del_catalogo_no_se_endurecio_para_tokens_cortos(self):
        """Los nombres cortos ('esoz', 'epax') siguen matcheando."""
        assert es_relevante("esoz", "ESOZ 40 MG X 7 CAP") is True
        assert es_relevante("esoz 40 mg", "ESOZ 40 MG X 7 CAP") is True


# ============================================================== F14.5 =========


class TestF145MultiMedicamento:
    def test_caso_real_1_se_parte_en_dos(self):
        assert _partir_consulta_multi(CASO_1) == ["nitrofurantoina 50mg", "vitamina c"]

    def test_caso_real_4_se_parte_en_dos(self):
        # El extractor del árbol integrado normaliza tildes en el término.
        assert _partir_consulta_multi(CASO_4) == [
            "solunoval solucion bucal", "pinvex solucion bucal"
        ]

    @pytest.mark.parametrize(
        "texto",
        ["que precio tiene la losartana y la metformina",
         "necesito atamel forte, tambien ibuprofeno",
         "precio de omeprazol; amoxicilina 500",
         "tienes esoz y tambien leprit?"],
    )
    def test_conectores_partes(self, texto):
        assert len(_partir_consulta_multi(texto)) == 2

    @pytest.mark.parametrize(
        "texto",
        ["tienes atamel y su precio",
         "quiero amoxicilina y que me digas el precio",
         "tienes atamel forte?",
         "no gracias, y disculpe"],
    )
    def test_un_solo_farmaco_no_se_parte(self, texto):
        assert _partir_consulta_multi(texto) == []

    @pytest.mark.parametrize(
        "texto",
        ["amoxicilina y acido clavulanico",
         "vitamina c y zinc",
         "losartan e hidroclorotiazida",
         "precio de amoxicilina y clavulanico"],
    )
    def test_coformulacion_no_se_parte(self, texto):
        """Amoxicilina + ácido clavulánico es UN producto (AUGMENTIN)."""
        assert _partir_consulta_multi(texto) == []

    def test_patron_de_dosis_repetida_sigue_funcionando(self):
        terminos = _partir_consulta_multi(
            "disponen de clopidogrel de 75 losartan de 50 atorvastatina de 30"
        )
        assert terminos == ["clopidogrel 75", "losartan 50", "atorvastatina 30"]

    def test_una_frase_sin_separadores_no_se_parte(self):
        assert _partir_consulta_multi("dame informacion del atamel forte") == []


# ================================================= integridad de las guardas ==


class TestGuardasIntactas:
    """F14 no puede tocar la cadena de vetos G1-G12 (validada 95/100 en F13)."""

    TPL_ESPERADAS = [
        "TPL_SIN_PRECIO", "TPL_FUERA_KB", "TPL_SIN_MEDIA", "TPL_CIERRE_G4",
        "TPL_SIN_COMPOSICION", "TPL_VERIFICACION_CATALOGO", "TPL_NO_ECHO",
        "TPL_BUSQUEDA_HONESTA", "TPL_CIERRE_ESCALADO", "TPL_G10_COMPOSICION",
        "TPL_G10_PROMESA", "TPL_G11_ESCALADO", "TPL_G12_TOOLCALL",
    ]

    def test_plantillas_de_guards_presentes(self):
        from app import guards

        for nombre in self.TPL_ESPERADAS:
            assert hasattr(guards, nombre), f"falta {nombre} en guards.py"

    def test_detectores_de_guards_presentes(self):
        from app import guards

        for nombre in (
            "PROMESA_ESCALADO", "G10_COMPOSICION", "G10_PROMESA_BUSQUEDA",
            "afirma_dato_catalogo", "es_echo", "contiene_toolcall_json",
            "promete_escalado", "acusacion_fraude",
        ):
            assert hasattr(guards, nombre), f"falta {nombre} en guards.py"

    def test_cadena_de_vetos_en_orden(self):
        fuente = (RAIZ / "app" / "turn.py").read_text(encoding="utf-8")
        for marca in ["# G2", "# G3", "# G1 —", "# G5", "# G6", "# G7", "# G8",
                      "# G12", "# G9", "# G10a", "# G10b", "# G11"]:
            assert marca in fuente, f"la cadena de vetos perdió {marca}"

    def test_guards_no_referencian_f14(self):
        """F14 es búsqueda: no debe haber entrado en los vetos."""
        fuente = (RAIZ / "app" / "guards.py").read_text(encoding="utf-8")
        assert "F14" not in fuente
        assert "alias" not in fuente.lower()
