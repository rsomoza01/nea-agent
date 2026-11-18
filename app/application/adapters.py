"""Adaptador compatible: expone `ToolRuntime` como `CatalogSearchPort`.

**No duplica** `ToolRuntime` ni ninguna dependencia: delega la búsqueda en
`ToolRuntime.execute("buscar_medicamento", ...)`, el mismo punto que usa hoy la ruta
caliente (`turn.py`). Por eso hereda, sin cambios, el manejo de `CrmError` que
`execute` traduce a `{ok: False, error: "crm_error"}` (`app/tools.py:2465`).
"""
from __future__ import annotations

from typing import Any


class ToolRuntimeCatalogAdapter:
    """Envuelve un `ToolRuntime` (del turno en curso) como puerto de búsqueda.

    Delega en `ToolRuntime.execute`, el punto público de despacho de herramientas, y
    por eso hereda su traducción de `CrmError` → `{ok:False, error:"crm_error"}`
    (`app/tools.py`). Es el adaptador para consumidores **externos** que quieran usar
    el caso de uso a través del despacho completo.

    ⚠ No lo use el propio despacho de `buscar_medicamento`: desde el Paso 5 `execute`
    enruta esa herramienta por el caso de uso (`ToolRuntime._buscar_catalogo`), de modo
    que un adaptador basado en `execute` reentraría en un bucle. Para el cableado
    interno existe `ToolRuntimeEngineCatalogAdapter`, que llama al motor directamente.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def buscar(self, termino: str) -> dict[str, Any]:
        return await self._runtime.execute("buscar_medicamento", {"nombre": termino})

    @property
    def aviso_forma(self) -> str:
        return getattr(self._runtime, "aviso_forma", "") or ""


class ToolRuntimeEngineCatalogAdapter:
    """Expone el **motor** de búsqueda (`ToolRuntime._buscar_medicamento`) como puerto.

    A diferencia de `ToolRuntimeCatalogAdapter`, llama al método del motor **directo**,
    no a `execute`. Es el puerto que usa el cableado de la ruta caliente
    (`ToolRuntime._buscar_catalogo`): como `execute` ahora despacha `buscar_medicamento`
    hacia el caso de uso, el adaptador debe saltarse `execute` para no reentrar.

    La traducción de `CrmError` se conserva igual que antes: el motor puede lanzar
    `CrmError`, que **propaga** a través del caso de uso (éste no la captura) hasta el
    `try/except CrmError` de `execute`, que la vuelve `{ok:False, error:"crm_error"}`.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def buscar(self, termino: str) -> dict[str, Any]:
        return await self._runtime._buscar_medicamento({"nombre": termino})

    @property
    def aviso_forma(self) -> str:
        return getattr(self._runtime, "aviso_forma", "") or ""
