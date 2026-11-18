"""F15 — regresiones de la capa de comprensión del acto de conversación.

Cubre: caminos rápidos, contrato JSON (incluido lo roto), fail-safe (timeout,
proveedor caído, JSON inválido, capa apagada), las 4 quejas reales y la
auditoría en `med_queries`. Ningún test llama a un LLM real: se inyecta un
`FakeLlm` con la respuesta (o el fallo) que se quiere medir.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.intencion import (
    ACTOS,
    Entendimiento,
    _extraer_json,
    _veredicto_desde_json,
    camino_rapido,
    entender,
)
from app.state import MemoryStore

RAIZ = Path(__file__).resolve().parents[1]


class FakeLlm:
    """LLM de mentira: devuelve `contenido`, lanza `error` o duerme `demora`."""

    def __init__(self, contenido: str | None = None, error: Exception | None = None,
                 demora: float = 0.0) -> None:
        self.contenido = contenido
        self.error = error
        self.demora = demora
        self.llamadas = 0
        self.ultimos_mensajes: list[dict] = []

    async def complete(self, messages, tools=None):  # noqa: ANN001
        self.llamadas += 1
        self.ultimos_mensajes = messages
        if self.demora:
            await asyncio.sleep(self.demora)
        if self.error:
            raise self.error

        class _R:
            content = self.contenido

        return _R()


class Settings:
    """Settings mínimos para la capa."""

    def __init__(self, **kw):
        self.intent_layer = True
        self.intent_model = ""
        self.intent_api_key = ""
        self.intent_base_url = ""
        self.intent_timeout = 12.0
        self.openai_api_key = "x"
        self.openai_base_url = ""
        for k, v in kw.items():
            setattr(self, k, v)


def _veredicto(acto: str, hay: bool, termino: str | None = None,
               confianza: float = 0.9) -> str:
    return json.dumps(
        {"acto": acto, "hay_consulta": hay, "termino": termino,
         "confianza": confianza, "motivo": "test"}
    )


# --------------------------------------------------------------- caminos rápidos ---

class TestCaminoRapido:
    def test_sin_termino_candidato_no_hay_camino_rapido(self):
        assert camino_rapido("tienes algo para la gripe?", None) is None

    @pytest.mark.parametrize("texto,candidato", [
        ("vivacina", "vivacina"),
        ("omeprasol", "omeprasol"),
        ("macrodantina 100", "macrodantina 100"),
    ])
    def test_nombre_pelado_corto_busca(self, texto, candidato):
        ent = camino_rapido(texto, candidato)
        assert ent is not None
        assert ent.hay_consulta is True
        assert ent.fuente == "nombre"

    @pytest.mark.parametrize("texto,candidato", [
        # Las 4 quejas reales.
        ("No puede ser Indu.", "ser indu"),
        ("Vivo en Ciudad Bolivar.", "vivo ciudad bolivar"),
        ("Solo una marca?", "marca"),
        ("Por ahroa no", "ahroa"),
        # Falsos positivos que destapó la sonda con el extractor real: llevan VERBO
        # de consulta pero son charla. Retirar el camino rápido por verbo los evita.
        ("Solo una marca o hay varias?", "marca varias"),
        ("Por ahora no necesito nada", "ahora"),
        ("tienen delivery?", "delivery"),
        ("aceptan zelle?", "aceptan zelle"),
        ("a que hora cierran hoy?", "hora cierran hoy"),
        ("gracias, linda", None),
        ("hola mi amor buenos dias", None),
    ])
    def test_charla_no_se_resuelve_con_heuristica(self, texto, candidato):
        """Ni por forma ni por verbo: estas frases las juzga la capa."""
        assert camino_rapido(texto, candidato) is None

    @pytest.mark.parametrize("texto,candidato", [
        # Sin evidencia (ni dosis ni forma farmacéutica) y corto: lo juzga la capa.
        ("shampo dreene", "shampo dreene"),
        ("atamel forte", "atamel forte"),
    ])
    def test_nombre_sin_evidencia_va_a_la_capa(self, texto, candidato):
        assert camino_rapido(texto, candidato) is None

    def test_el_verbo_de_consulta_ya_no_decide_solo(self):
        """Regresión del fallo medido: 'tienen delivery?' disparaba búsqueda."""
        from app.turn import _extraer_termino_medicamento

        for frase in ("tienen delivery?", "aceptan zelle?", "por ahora no necesito nada"):
            candidato = _extraer_termino_medicamento(frase)
            assert camino_rapido(frase, candidato) is None, frase


# --------------------------------------------------------------------- contrato ---

class TestContratoJson:
    def test_tolera_cercas_de_markdown(self):
        texto = 'ok\n```json\n{"acto": "saludo", "hay_consulta": false}\n```'
        assert _extraer_json(texto) == {"acto": "saludo", "hay_consulta": False}

    def test_tolera_prologo_y_cola(self):
        datos = _extraer_json('Claro: {"acto":"otro","hay_consulta":false} fin')
        assert datos == {"acto": "otro", "hay_consulta": False}

    def test_llaves_dentro_de_cadena_no_confunden(self):
        datos = _extraer_json('{"motivo":"dice { que no }","acto":"otro"}')
        assert datos is not None and datos["motivo"] == "dice { que no }"

    def test_json_roto_devuelve_none(self):
        assert _extraer_json("{no es json") is None
        assert _extraer_json("") is None

    def test_contratos_invalidos_se_rechazan(self):
        """Revisión externa: '{"motivo":"x"}' no puede ser una negativa concluyente."""
        invalidos = [
            {"acto": "fiesta", "hay_consulta": False},                      # acto inválido
            {"acto": "otro", "hay_consulta": "true"},                       # bool como cadena
            {"acto": "otro"},                                               # sin hay_consulta
            {"acto": "consulta_medicamento", "hay_consulta": True},         # consulta sin término
            {"acto": "consulta_medicamento", "hay_consulta": False},        # incoherente
            {"acto": "otro", "hay_consulta": False, "confianza": "alta"},   # confianza no numérica
            {"acto": "otro", "hay_consulta": False, "confianza": 7},        # fuera de rango
            {"motivo": "x"},                                                # sin acto ni bool
        ]
        for datos in invalidos:
            assert _veredicto_desde_json(datos) is None, datos

    def test_contrato_valido_limpia_termino_y_conserva_confianza(self):
        v = _veredicto_desde_json(
            {"acto": "consulta_medicamento", "hay_consulta": True,
             "termino": "  atamel   forte. ", "confianza": 0.9, "motivo": "  pide atamel "}
        )
        assert v is not None
        assert v.termino == "atamel forte"
        assert v.confianza == 0.9 and v.motivo == "pide atamel"

    def test_termino_se_ignora_si_no_hay_consulta(self):
        v = _veredicto_desde_json(
            {"acto": "dato_personal", "hay_consulta": False, "termino": "bolivar",
             "confianza": 0.8}
        )
        assert v is not None and v.termino is None

    def test_actos_declarados(self):
        assert "consulta_medicamento" in ACTOS and "atributo_producto" in ACTOS


# ------------------------------------------------------------------------ capa ---

class TestCapa:
    def test_capa_no_se_llama_cuando_el_camino_rapido_ya_decidio(self):
        llm = FakeLlm(_veredicto("otro", False))
        ent = asyncio.run(entender("vivacina", llm=llm, settings=Settings(),
                                   termino_candidato="vivacina"))
        assert ent.hay_consulta is True
        assert llm.llamadas == 0, "el camino rápido no debe gastar una llamada"

    def test_queja_del_laboratorio_no_es_consulta(self):
        """«No puede ser Indu.» — el cliente habla del laboratorio."""
        llm = FakeLlm(_veredicto("atributo_producto", False))
        ent = asyncio.run(entender("No puede ser Indu.", llm=llm, settings=Settings(),
                                   termino_candidato="ser indu"))
        assert ent.hay_consulta is False
        assert ent.acto == "atributo_producto"
        assert ent.termino is None
        assert llm.llamadas == 1

    @pytest.mark.parametrize("texto,candidato,acto", [
        ("Vivo en Ciudad Bolivar.", "vivo ciudad bolivar", "dato_personal"),
        ("Solo una marca?", "marca", "atributo_producto"),
        ("Por ahroa no", "ahroa", "dilatoria"),
        ("que Dios te bendiga", None, "cortesia"),
    ])
    def test_las_otras_quejas_tampoco_disparan_busqueda(self, texto, candidato, acto):
        llm = FakeLlm(_veredicto(acto, False))
        ent = asyncio.run(entender(texto, llm=llm, settings=Settings(),
                                  termino_candidato=candidato))
        assert ent.hay_consulta is False and ent.acto == acto

    def test_consulta_legitima_con_contexto_si_busca(self):
        """Frase larga sin verbo de consulta: no hay camino rápido, decide la capa."""
        llm = FakeLlm(_veredicto("consulta_medicamento", True, "diclofenaco 50", 0.85))
        ent = asyncio.run(entender("el diclofencao de 50 ese lo llevo yo",
                                  llm=llm, settings=Settings(),
                                  termino_candidato="diclofencao 50",
                                  producto_previo="MACRODANTINA 100 MG X 20 CAP"))
        assert ent.hay_consulta is True
        assert ent.termino == "diclofenaco 50"
        assert ent.fuente == "capa"
        assert llm.llamadas == 1

    def test_mensaje_con_verbo_lo_juzga_la_capa(self):
        """El verbo ya no decide: 'precio de X' va a la capa, que confirma el término."""
        llm = FakeLlm(_veredicto("consulta_medicamento", True, "macrodantina 100", 0.9))
        ent = asyncio.run(entender("precio de la macrodantina de 100",
                                  llm=llm, settings=Settings(),
                                  termino_candidato="macrodantina"))
        assert ent.hay_consulta is True and ent.fuente == "capa"
        assert ent.termino == "macrodantina 100"
        assert llm.llamadas == 1

    def test_la_capa_recibe_contexto_util(self):
        llm = FakeLlm(_veredicto("atributo_producto", False))
        asyncio.run(entender("Solo una marca?", llm=llm, settings=Settings(),
                             termino_candidato="marca", producto_previo="ATAMEL FORTE"))
        prompt = llm.ultimos_mensajes[-1]["content"]
        assert "Solo una marca?" in prompt
        assert "ATAMEL FORTE" in prompt          # producto mostrado antes
        assert "marca" in prompt                  # candidato sospechoso, para juzgarlo

    # ----------------------------------------------------------------- fail-safe ---

    def test_failsafe_si_el_proveedor_falla(self):
        llm = FakeLlm(error=RuntimeError("connection error"))
        ent = asyncio.run(entender("Solo una marca?", llm=llm, settings=Settings(),
                                  termino_candidato="marca"))
        assert ent.hay_consulta is None and ent.fuente == "fail-safe"

    def test_failsafe_si_el_json_es_invalido(self):
        llm = FakeLlm("perdón, no entendí la pregunta")
        ent = asyncio.run(entender("Solo una marca?", llm=llm, settings=Settings(),
                                  termino_candidato="marca"))
        assert ent.hay_consulta is None and ent.fuente == "fail-safe"

    def test_failsafe_por_timeout(self):
        llm = FakeLlm(_veredicto("otro", False), demora=0.5)
        ent = asyncio.run(entender("Solo una marca?", llm=llm, settings=Settings(intent_timeout=0.05),
                                  termino_candidato="marca"))
        assert ent.hay_consulta is None and ent.fuente == "fail-safe"
        assert "timeout" in ent.motivo

    def test_capa_apagada_no_es_concluyente(self):
        llm = FakeLlm(_veredicto("otro", False))
        ent = asyncio.run(entender("Solo una marca?", llm=llm, settings=Settings(intent_layer=False),
                                  termino_candidato="marca"))
        assert ent.hay_consulta is None and ent.fuente == "desactivada"
        assert llm.llamadas == 0

    def test_confianza_baja_no_es_concluyente(self):
        """Revisión externa: la confianza no puede ser decorativa."""
        llm = FakeLlm(json.dumps({"acto": "consulta_medicamento", "hay_consulta": True,
                                  "termino": "atamel", "confianza": 0.2,
                                  "motivo": "no estoy seguro"}))
        ent = asyncio.run(entender("atamel?", llm=llm, settings=Settings(),
                                   termino_candidato="atamel"))
        assert ent.hay_consulta is None and ent.fuente == "fail-safe"
        assert "confianza baja" in ent.motivo

    def test_contrato_incoherente_es_no_concluyente(self):
        llm = FakeLlm('{"acto": "consulta_medicamento", "hay_consulta": true, "confianza": 0.9}')
        ent = asyncio.run(entender("atamel?", llm=llm, settings=Settings(),
                                   termino_candidato="atamel"))
        assert ent.hay_consulta is None, "consulta sin término no puede decidir"

    def test_timeout_cero_o_negativo_usa_default(self):
        llm = FakeLlm(_veredicto("saludo", False), demora=0.05)
        ent = asyncio.run(entender("hola", llm=llm, settings=Settings(intent_timeout=0),
                                   termino_candidato=None))
        # No revienta con timeout=0: usa el default (12s) y el LLM responde.
        assert ent.hay_consulta is False and ent.fuente == "capa"

    def test_entendimiento_concluyente(self):
        assert Entendimiento(hay_consulta=False).concluyente
        assert Entendimiento(hay_consulta=True).concluyente
        assert not Entendimiento(hay_consulta=None).concluyente


# -------------------------------------------------------------------- auditoría ---

class TestAuditoria:
    def test_med_queries_guarda_el_veredicto(self):
        store = MemoryStore()
        asyncio.run(store.log_med_query(
            conversation_id=1, provider_id="demo", term="atamel 500",
            product_id="p1", product_name="ATAMEL 500", result_count=1,
            intent={"fuente": "capa", "acto": "consulta_medicamento",
                    "motivo": "pedido claro", "confianza": 0.91},
        ))
        fila = store.med_queries[-1]
        assert fila["intent"]["fuente"] == "capa"
        assert fila["intent"]["confianza"] == 0.91

    def test_decision_sin_busqueda_tambien_se_registra(self):
        """Revisión externa #6: los rechazos (no buscar) también dejan rastro."""
        store = MemoryStore()
        asyncio.run(store.log_intent_decision(
            conversation_id=1, provider_id="demo", fuente="capa", acto="dato_personal",
            hay_consulta=False, termino=None, candidato="vivo ciudad bolivar",
            motivo="da un dato personal", confianza=0.92,
        ))
        fila = store.intent_decisions[-1]
        assert fila["hay_consulta"] is False and fila["fuente"] == "capa"
        assert fila["candidato"] == "vivo ciudad bolivar"

    def test_evidencia_de_producto(self):
        """El atajo de la autorización central: dosis con unidad o forma farmacéutica."""
        from app.tools import _tiene_evidencia_de_producto

        for si in ("atamel 500 mg", "jarabe para la tos", "crema", "losartan 50mg",
                   "vitamina c 500", "gotas"):
            assert _tiene_evidencia_de_producto(si), si
        for no in ("ladrones", "estafa", "delivery", "puedo devolverlas", "marca"):
            assert not _tiene_evidencia_de_producto(no), no
        # F15.1 — un documento NO es una dosis: el OCR de una receta pone delante del modelo
        # el nombre del paciente y su colegiatura/cédula, y eso llegaba al catálogo.
        for documento in ("maria fernandez cmp 45231", "cedula 12345678",
                          "dra maria fernandez colegiatura 123456", "firma 20261"):
            assert not _tiene_evidencia_de_producto(documento), documento

    def test_med_queries_sin_capa_no_revienta(self):
        store = MemoryStore()
        asyncio.run(store.log_med_query(
            conversation_id=1, provider_id="demo", term="x", product_id=None,
            product_name=None, result_count=0,
        ))
        assert store.med_queries[-1]["intent"] == {}


# --------------------------------------------------------------- cableado (turn) ---

class TestCableadoTurno:
    def test_la_capa_es_el_decisor_y_el_heuritico_solo_failsafe(self):
        fuente = (RAIZ / "app" / "turn.py").read_text(encoding="utf-8")
        assert "entender_intencion(" in fuente
        # El heurístico de forma sobrevive SOLO en los caminos sin capa (apagada/fail-safe).
        assert fuente.count("_parece_consulta_medicamento(user_text)") <= 2
        assert "intent_layer" in fuente

    def test_migracion_de_auditoria_existe(self):
        sql = (RAIZ / "migrations" / "014_med_queries_intent.sql").read_text(encoding="utf-8")
        for columna in ("intent_fuente", "intent_acto", "intent_motivo", "intent_confianza"):
            assert columna in sql

    def test_la_capa_no_toca_los_guards(self):
        """La capa decide ANTES de buscar: `guards.py` no debe CONOCERLA.

        Nota: `guards.py` ya tenía un `intencion_fuera_kb` de antes de F15 (su propio
        filtro léxico); lo que no puede aparecer es un acoplamiento con la capa nueva.
        """
        guards = (RAIZ / "app" / "guards.py").read_text(encoding="utf-8")
        assert "app.intencion" not in guards
        assert "entender_intencion" not in guards
        assert "intent_layer" not in guards


# ------------------------------------------------ autorización central de tools ---

class TestAutorizacionCentral:
    def _runtime_minimo(self):
        from app.tools import ToolRuntime

        runtime = ToolRuntime.__new__(ToolRuntime)
        runtime.intent_fuente = ""
        runtime.intent_acto = ""
        runtime.intent_motivo = ""
        runtime.intent_confianza = None
        runtime.intent_hay_consulta = None
        runtime.intent_termino = None
        runtime.intent_autorizado = None
        runtime.texto_cliente = "puedo devolverlas?"
        runtime.last_product = None
        runtime.decision_registrada = False

        async def registrar(candidato=None):
            runtime.decision_registrada = True

        runtime.registrar_decision_intencion = registrar
        return runtime

    def test_import_error_deniega_busqueda(self, monkeypatch):
        import builtins

        runtime = self._runtime_minimo()
        original_import = builtins.__import__

        def fallo_intencion(name, *args, **kwargs):
            if name == "app.intencion":
                raise ImportError("simulated import failure")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fallo_intencion)
        resultado = asyncio.run(runtime._autorizar_busqueda("devolverlas"))
        assert resultado["error"] == "no_autorizado_por_comprension"
        assert runtime.intent_autorizado is False
        assert runtime.intent_fuente == "fail-safe"
        assert runtime.decision_registrada is True

    def test_error_inesperado_deniega_busqueda(self, monkeypatch):
        import app.intencion

        runtime = self._runtime_minimo()

        async def fallo(*args, **kwargs):
            raise RuntimeError("simulated provider failure")

        monkeypatch.setattr(app.intencion, "entender", fallo)
        resultado = asyncio.run(runtime._autorizar_busqueda("devolverlas"))
        assert resultado["error"] == "no_autorizado_por_comprension"
        assert runtime.intent_autorizado is False
        assert runtime.intent_fuente == "fail-safe"
        assert runtime.decision_registrada is True


# --------------------------------------- autorización POR TÉRMINO (F15.1) ---

class TestAutorizacionPorTermino:
    """F15.1 — la puerta autoriza cada TÉRMINO, no el turno entero.

    Hueco medido (PENDIENTE #1 de F15): con el turno ya autorizado, las búsquedas que el
    MODELO lanzaba después (`vitamina`, `acabo`, `ustedes`) pasaban la puerta y sus
    resultados llegaban al cliente — listados de VITAMINA C/K/E partiendo de «tienes
    vivacina?». Y `sugerir_generico` era una segunda puerta al catálogo sin candado.
    """

    def _runtime(self, **campos):
        from app.tools import ToolRuntime

        runtime = ToolRuntime.__new__(ToolRuntime)
        runtime.intent_fuente = ""
        runtime.intent_acto = ""
        runtime.intent_motivo = ""
        runtime.intent_confianza = None
        runtime.intent_hay_consulta = None
        runtime.intent_termino = None
        runtime.intent_autorizado = None
        runtime.texto_cliente = ""
        runtime.last_product = None
        runtime.intent_terminos_autorizados = set()
        runtime.intent_rechazos = {}
        runtime.decisiones = []
        for k, v in campos.items():
            setattr(runtime, k, v)

        class _Ctx:
            llm = None
            settings = None

        runtime._ctx = _Ctx()

        async def registrar_decision_intencion(candidato=None):
            runtime.decisiones.append(candidato)

        runtime.registrar_decision_intencion = registrar_decision_intencion
        return runtime

    def _turno_autorizado(self, termino: str = "losartan", **campos):
        """Estado del turno JUSTO después de aprobar `termino` (lo que deja la puerta)."""
        return self._runtime(
            texto_cliente="tienes losartan 50 mg?",
            intent_fuente="capa",
            intent_acto="consulta_medicamento",
            intent_motivo="consulta por losartan",
            intent_confianza=0.95,
            intent_hay_consulta=True,
            intent_termino=termino,
            intent_autorizado=True,
            intent_terminos_autorizados={termino},
            **campos,
        )

    def _entender_falso(self, monkeypatch, *respuestas, registro=None):
        """Sustituye la capa: devuelve `respuestas` en orden y anota las llamadas."""
        import app.intencion

        cola = list(respuestas)

        async def falso(texto, **kwargs):
            if registro is not None:
                registro.append({"texto": texto, **kwargs})
            if not cola:
                raise AssertionError("la capa no debía ser llamada otra vez")
            return cola.pop(0)

        monkeypatch.setattr(app.intencion, "entender", falso)

    # --- el mismo término y el mismo producto: nunca pagan llamada -----------------

    def test_mismo_termino_no_vuelve_a_llamar_a_la_capa(self, monkeypatch):
        import app.intencion

        runtime = self._turno_autorizado("losartan")

        async def no_deberia_clasificar(*args, **kwargs):
            raise AssertionError("el término ya estaba autorizado en este turno")

        monkeypatch.setattr(app.intencion, "entender", no_deberia_clasificar)
        assert asyncio.run(runtime._autorizar_busqueda("LOSARTAN")) is None

    def test_mismo_producto_con_otro_formato_no_llama_a_la_capa(self, monkeypatch):
        """«losartan 50 mg» tras aprobar «losartan 50»: el modelo añadió la dosis."""
        import app.intencion

        runtime = self._turno_autorizado("losartan 50")

        async def no_deberia_clasificar(*args, **kwargs):
            raise AssertionError("es el mismo producto, no un término nuevo")

        monkeypatch.setattr(app.intencion, "entender", no_deberia_clasificar)
        assert asyncio.run(runtime._autorizar_busqueda("losartan 50 mg")) is None

    # --- término nuevo: decide la capa, con el mensaje del cliente -----------------

    def test_termino_distinto_lo_decide_la_capa(self, monkeypatch):
        llamadas: list[dict] = []
        self._entender_falso(
            monkeypatch,
            Entendimiento(acto="consulta_medicamento", hay_consulta=True,
                          termino="metformin", confianza=0.9, motivo="el cliente lo pidió",
                          fuente="capa"),
            registro=llamadas,
        )
        runtime = self._turno_autorizado("losartan")
        runtime.texto_cliente = "necesito losartan y metformin"

        assert asyncio.run(runtime._autorizar_busqueda("metformin")) is None
        assert len(llamadas) == 1
        # La capa ve el MENSAJE del cliente y el término candidato: con eso decide.
        assert llamadas[0]["texto"] == "necesito losartan y metformin"
        assert llamadas[0]["termino_candidato"] == "metformin"
        # Y se le pregunta por ESE término, no por el mensaje (F15.1): sin esto un «sí» del
        # mensaje autorizaba el término que el modelo eligiera (`oiga`, `firma`).
        assert llamadas[0]["candidato_obligatorio"] is True
        assert "metformin" in runtime.intent_terminos_autorizados
        # Y queda registrada la decisión (auditoría).
        assert runtime.decisiones == ["metformin"]

    @pytest.mark.parametrize("termino", ["vitamina", "acabo", "ustedes"])
    def test_termino_inventado_lo_rechaza_la_capa(self, monkeypatch, termino):
        self._entender_falso(
            monkeypatch,
            Entendimiento(acto="dato_personal", hay_consulta=False, termino=None,
                          confianza=0.9, motivo="no lo pidió el cliente", fuente="capa"),
        )
        runtime = self._turno_autorizado("vivacina")
        runtime.texto_cliente = "tienen vivacina?"

        resultado = asyncio.run(runtime._autorizar_busqueda(termino))
        assert resultado is not None
        assert resultado["error"] == "no_autorizado_por_comprension"
        assert runtime.intent_autorizado is False
        assert runtime.decisiones == [termino]

    def test_termino_rechazado_no_se_vuelve_a_pagar(self):
        """Insistir con el MISMO término ya rechazado no cuesta otra llamada."""
        import app.intencion

        runtime = self._turno_autorizado("vivacina")
        runtime.texto_cliente = "tienen vivacina?"
        runtime.intent_rechazos["vitamina"] = ("otro", "no lo pidió el cliente")
        llamado = False

        async def no_deberia_clasificar(*args, **kwargs):
            nonlocal llamado
            llamado = True
            raise AssertionError("ya se rechazó este término en el turno")

        original = app.intencion.entender
        app.intencion.entender = no_deberia_clasificar
        try:
            resultado = asyncio.run(runtime._autorizar_busqueda("VITAMINA"))
        finally:
            app.intencion.entender = original

        assert resultado is not None and not llamado

    # --- el veredicto del backstop se hereda solo para SU término ------------------

    def test_termino_ya_negado_por_el_backstop_no_se_rescata(self, monkeypatch):
        import app.intencion

        runtime = self._runtime(
            texto_cliente="usted es una estafa, no me llego",
            intent_fuente="capa", intent_acto="reclamo", intent_motivo="reclamo",
            intent_confianza=0.9, intent_hay_consulta=False, intent_termino="estafa",
            intent_autorizado=False,
        )

        async def no_deberia_clasificar(*args, **kwargs):
            raise AssertionError("el backstop ya dictó que 'estafa' no es consulta")

        monkeypatch.setattr(app.intencion, "entender", no_deberia_clasificar)
        resultado = asyncio.run(runtime._autorizar_busqueda("estafa"))
        assert resultado is not None
        assert resultado["error"] == "no_autorizado_por_comprension"

    # --- fail-closed: sigue siendo cerrado, pero no castiga lo ya aprobado ---------

    def test_fail_closed_deniega_el_termino_nuevo(self, monkeypatch):
        import builtins

        runtime = self._turno_autorizado("losartan")
        original = builtins.__import__

        def fallo(name, *args, **kwargs):
            if name == "app.intencion":
                raise ImportError("simulated import failure")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fallo)
        resultado = asyncio.run(runtime._autorizar_busqueda("vitamina"))
        assert resultado is not None
        assert runtime.intent_fuente == "fail-safe"
        assert runtime.intent_autorizado is False

    def test_fail_closed_no_repite_lo_ya_autorizado(self, monkeypatch):
        import builtins

        runtime = self._turno_autorizado("losartan")
        original = builtins.__import__

        def fallo(name, *args, **kwargs):
            if name == "app.intencion":
                raise ImportError("simulated import failure")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fallo)
        assert asyncio.run(runtime._autorizar_busqueda("losartan")) is None

    # --- segunda puerta: sugerir_generico ----------------------------------------

    def _runtime_con_catalogo(self, **campos):
        runtime = self._runtime(_provider_id_val="demo", **campos)
        consultas: list[str] = []

        class _Crm:
            async def get_products(self, provider_id, q=None, limit=8):
                consultas.append(q or "")
                return {"products": [{"producto": "X", "generico": "generico-x",
                                      "precio": 1.0}], "provider": {}}

        class _Ctx:
            llm = None
            settings = None
            crm = _Crm()
            store = None

        runtime._ctx = _Ctx()
        return runtime, consultas

    def test_sugerir_generico_no_consulta_sin_autorizacion(self):
        import app.intencion

        runtime, consultas = self._runtime_con_catalogo(
            texto_cliente="tienen vivacina?",
        )
        original = app.intencion.entender

        async def no_deberia_clasificar(*args, **kwargs):
            raise AssertionError("sin capa viva, la puerta es fail-closed")

        app.intencion.entender = no_deberia_clasificar
        try:
            resultado = asyncio.run(runtime._sugerir_generico({"nombre": "vitamina"}))
        finally:
            app.intencion.entender = original

        assert resultado["ok"] is False
        assert resultado["error"] == "no_autorizado_por_comprension"
        assert consultas == []

    def test_sugerir_generico_consulta_cuando_el_termino_esta_autorizado(self):
        runtime, consultas = self._runtime_con_catalogo(
            texto_cliente="busco un ibuprofeno generico",
            intent_fuente="capa", intent_acto="consulta_medicamento",
            intent_motivo="consulta por ibuprofeno", intent_confianza=0.9,
            intent_hay_consulta=True, intent_termino="ibuprofeno",
            intent_autorizado=True,
            intent_terminos_autorizados={"ibuprofeno"},
        )
        resultado = asyncio.run(runtime._sugerir_generico({"nombre": "ibuprofeno"}))
        assert resultado.get("ok") is True
        assert consultas == ["ibuprofeno"]
