"""Puertos mínimos del caso de uso de búsqueda de catálogo (UC-01).

Se define **un** puerto, al nivel del **motor de búsqueda** existente, no a nivel del
CRM crudo. El algoritmo de búsqueda (typos, alias, principio activo, forma, relevancia)
está entrelazado en `ToolRuntime._buscar_medicamento` (~930 líneas); un puerto a nivel
de `crm.get_products` obligaría a reimplementar ese algoritmo, que es el Paso 5. Ver
`docs/architecture/PR-refactor-application-contracts.md` (bloqueo documentado).

El puerto es un `Protocol` estructural: el adaptador existente lo satisface sin que se
duplique ninguna clase (`CrmClient`, `Store`, `ToolRuntime`, LLM).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CatalogSearchPort(Protocol):
    """Capacidad de buscar un medicamento en el catálogo de la farmacia.

    `buscar` ejecuta la búsqueda del término (incluida la autorización F15.1, que
    sigue siendo responsabilidad del motor subyacente) y devuelve el dict crudo del
    handler. `aviso_forma` expone, tras la llamada, el aviso de forma farmacéutica
    que el handler deja fuera del dict de éxito.
    """

    async def buscar(self, termino: str) -> dict[str, Any]:
        ...

    @property
    def aviso_forma(self) -> str:
        ...
