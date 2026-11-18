"""Contratos tipados del caso de uso de búsqueda de catálogo (UC-01).

Formaliza, de forma **aditiva**, los resultados que hoy devuelve el handler real
`app.tools.ToolRuntime._buscar_medicamento` como `dict {ok, ...}` heterogéneos
(ver `docs/architecture/TO-BE.md` §4 y `docs/architecture/USE-CASES.md` UC-01).

No reimplementa la búsqueda: solo **describe** su salida. `CatalogSearchResult.raw`
conserva el dict original intacto y `to_tool_dict()` lo devuelve sin tocar, de modo
que cualquier consumidor que hoy espere el dict (p. ej. el LLM vía `instrucciones`)
siga recibiendo exactamente la misma serialización.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CatalogSearchStatus(str, Enum):
    """Resultados tipados de UC-01, mapeados contra el handler AS-IS.

    Es un subconjunto de los resultados comunes del TO-BE §4: declara solo los que
    el handler real emite hoy. `AMBIGUO` queda **reservado** (decisión del propietario,
    Paso 5): el contrato NO selecciona automáticamente entre varios candidatos
    plausibles; esa resolución exige una regla explícita y evidencia suficiente. Mientras
    tanto, "varias opciones / forma no resuelta" se presenta como `PARCIAL` vía
    `aviso_forma` y la redacción debe ser conservadora o pedir precisión.

    No hay estado `EQUIVALENTE` (decisión del propietario, Paso 5): compartir principio
    activo NO demuestra equivalencia farmacéutica/terapéutica. La coincidencia por
    principio activo se tipa como `PARCIAL` y se hace explícita, SIN elevarla a
    equivalencia, con `por_principio_activo=True` + la limitación
    `equivalencia_no_confirmada` (ver `_limitaciones`). Sugerir un genérico es otra
    responsabilidad (`sugerir_generico`), con reglas y evidencia propias, y NO se deriva
    automáticamente de compartir principio activo (se extraerá en un paso posterior).
    """

    ENCONTRADO = "encontrado"
    PARCIAL = "parcial"
    NO_ENCONTRADO = "no_encontrado"
    NO_AUTORIZADO = "no_autorizado"
    NO_CONSULTA = "no_consulta"
    PROVEEDOR_NO_DISPONIBLE = "proveedor_no_disponible"
    ERROR = "error"  # error no reconocido del handler — no se asume que sea NO_CONSULTA
    AMBIGUO = "ambiguo"  # reservado — sin fuente distinta en el AS-IS actual


# Errores internos del handler que significan "el término NO es una consulta de
# medicamento buscable" (no se tocó el catálogo). Se agrupan en NO_CONSULTA.
_ERRORES_NO_CONSULTA = frozenset(
    {"nombre_vacio", "solo_sal", "sin_medicamento", "solo_saludo", "no_medicamento"}
)

# Errores con mapeo directo a un estado tipado.
_ERRORES_DIRECTOS: dict[str, CatalogSearchStatus] = {
    "sin_resultados": CatalogSearchStatus.NO_ENCONTRADO,
    "no_autorizado_por_comprension": CatalogSearchStatus.NO_AUTORIZADO,
    "crm_error": CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE,
}

# Única fuente de datos de producto de UC-01: el catálogo de la farmacia, expuesto
# por el CRM (`CrmClient.get_products`, ver DOMAIN-MODEL §4.1/§6). El caso de uso no
# inventa datos ni los toma de otra fuente.
FUENTE_CATALOGO = "catalogo_farmacia"

# Estados en los que el catálogo SÍ se consultó (o se intentó consultar): tienen
# `fuente`. NO_AUTORIZADO (F15.1 vetó antes de tocar el catálogo), NO_CONSULTA (no es
# una consulta de medicamento) y ERROR (origen no reconocido) NO tienen fuente.
_ESTADOS_CON_FUENTE = frozenset(
    {
        CatalogSearchStatus.ENCONTRADO,
        CatalogSearchStatus.PARCIAL,
        CatalogSearchStatus.NO_ENCONTRADO,
        CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE,
    }
)


def _campos_disponibles(productos: list[dict[str, Any]]) -> tuple[str, ...]:
    """Campos de catálogo realmente presentes en los productos (la evidencia que hay).

    Es la unión ordenada de las claves de todos los productos. Documenta qué datos
    respaldan una afirmación (precio, disponibilidad, presentación, …) sin asumir
    campos que el catálogo no trajo (DOMAIN-MODEL §4.1: campos distintos, nunca
    inferidos)."""
    campos: set[str] = set()
    for p in productos:
        if isinstance(p, dict):
            campos.update(p.keys())
    return tuple(sorted(campos))


def _tiene_precio_bs(p: dict[str, Any]) -> bool:
    valor = p.get("precioBs")
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and valor > 0


def _limitaciones(
    status: CatalogSearchStatus,
    productos: list[dict[str, Any]],
    aviso: str,
    por_principio_activo: bool,
) -> tuple[str, ...]:
    """Límites de evidencia del resultado (prompt punto 6, DOMAIN-MODEL §6).

    Hace explícito qué NO está respaldado por el producto, para que el consumidor no
    afirme de más:
    - `equivalencia_no_confirmada`: el resultado salió del principio activo inferido,
      que es **mitigación, no evidencia** de equivalencia (no hay fuente de verdad).
    - `forma_pedida_no_disponible`: la forma/presentación pedida no está; se ofrecen
      otras (el `aviso_forma` debe decirlo).
    - `precio_bs_incompleto`: algún producto no trae precio en Bs (no se inventa 0)."""
    lims: list[str] = []
    if por_principio_activo:
        lims.append("equivalencia_no_confirmada")
    if aviso:
        lims.append("forma_pedida_no_disponible")
    if status in (CatalogSearchStatus.ENCONTRADO, CatalogSearchStatus.PARCIAL):
        productos_dict = [p for p in productos if isinstance(p, dict)]
        if productos_dict and not all(_tiene_precio_bs(p) for p in productos_dict):
            lims.append("precio_bs_incompleto")
    return tuple(lims)


class ApplicationError(Exception):
    """Error de la capa de aplicación (base)."""


class ProveedorNoDisponibleError(ApplicationError):
    """Una dependencia externa (CRM/LLM) no está disponible o agotó su tiempo.

    El adaptador por defecto (`ToolRuntimeCatalogAdapter`) NO la lanza —delega en
    `ToolRuntime.execute`, que ya traduce `CrmError` a `{ok:False,error:"crm_error"}`—,
    pero un adaptador que envuelva infraestructura cruda puede lanzarla; el caso de
    uso la normaliza a `PROVEEDOR_NO_DISPONIBLE` sin filtrar la excepción de infra.
    """


@dataclass(frozen=True)
class CatalogSearchRequest:
    """Entrada mínima del caso de uso: el término tal como se pidió buscar."""

    termino: str


@dataclass(frozen=True)
class CatalogSearchResult:
    """Resultado tipado de una búsqueda de catálogo.

    `raw` es el dict exacto devuelto por el handler/adaptador; `to_tool_dict()` lo
    devuelve sin modificar para preservar la serialización observable.
    """

    status: CatalogSearchStatus
    termino: str
    productos: list[dict[str, Any]] = field(default_factory=list)
    detalle: str | None = None
    aviso_forma: str | None = None
    por_principio_activo: bool = False
    # Metadatos estructurados (prompt punto 5). Son **derivados** del resultado, no
    # cambian la serialización observable (`raw`/`to_tool_dict()`):
    #   fuente             — de dónde salió la evidencia (el catálogo) o None.
    #   campos_disponibles — qué campos de catálogo trajo cada producto.
    #   limitaciones       — qué NO está respaldado (equivalencia, forma, Bs).
    fuente: str | None = None
    campos_disponibles: tuple[str, ...] = ()
    limitaciones: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def to_tool_dict(self) -> dict[str, Any]:
        """Serialización observable original (identidad), para consumidores legados."""
        return self.raw


def mapear_resultado(
    raw: dict[str, Any], *, aviso_forma: str = "", termino: str = ""
) -> CatalogSearchResult:
    """Traduce el dict del handler a un `CatalogSearchResult` tipado (función pura).

    `aviso_forma` se pasa aparte porque el dict de éxito no lo incluye: el handler lo
    funde en `instrucciones` y lo deja en el flag `runtime.aviso_forma`.
    """
    raw = raw or {}
    productos = list(raw.get("products") or [])
    detalle = raw.get("detalle")
    aviso = (aviso_forma or "").strip()
    por_principio_activo = False

    if raw.get("ok"):
        if raw.get("principio_activo"):
            # Marca exacta ausente; se ofrecen alternativas por principio activo.
            status = CatalogSearchStatus.PARCIAL
            por_principio_activo = True
        elif aviso:
            # Hay producto pero no en la forma pedida; se muestran alternativas.
            status = CatalogSearchStatus.PARCIAL
        else:
            status = CatalogSearchStatus.ENCONTRADO
    else:
        error = raw.get("error")
        if error in _ERRORES_DIRECTOS:
            status = _ERRORES_DIRECTOS[error]
        elif error in _ERRORES_NO_CONSULTA:
            status = CatalogSearchStatus.NO_CONSULTA
        else:
            # Un error no catalogado NO se asume como NO_CONSULTA: podría ser un fallo
            # real que quedaría enmascarado como "el cliente no pidió un medicamento".
            # Se tipa como ERROR explícito para que el consumidor lo trate como anomalía.
            status = CatalogSearchStatus.ERROR

    fuente = FUENTE_CATALOGO if status in _ESTADOS_CON_FUENTE else None
    return CatalogSearchResult(
        status=status,
        termino=termino,
        productos=productos,
        detalle=detalle,
        aviso_forma=aviso or None,
        por_principio_activo=por_principio_activo,
        fuente=fuente,
        campos_disponibles=_campos_disponibles(productos),
        limitaciones=_limitaciones(status, productos, aviso, por_principio_activo),
        raw=raw,
    )
