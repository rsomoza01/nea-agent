"""Superficie Staff de NEA (Paso 11).

Agente/API interna para empleados de la farmacia, montada SOBRE una identidad
verificable de empleado (ADR-0010) que desbloquea el BLOQUEO de ADR-0008. Separada
a propósito de la superficie Customer (`app/turn.py`, `app/tools.py`): el cliente
jamás alcanza estas operaciones; cada operación comprueba actor, rol, farmacia,
recurso y precondición en servidor, deny-by-default, y audita todo.

Submódulos:
  - `identity`: modelos de empleado/sesión, hashing de credenciales, emisión/verificación
    de sesión y el `StaffStore` (persistencia de identidad/sesión/auditoría).
  - `policy`: `AuthorizationContext`, decisión tipada deny-by-default y el catálogo
    cerrado de operaciones internas clasificadas por riesgo.
  - `api`: router FastAPI `/staff/*` (login + piloto cerrado de operaciones).
  - `pg`: implementación Postgres del `StaffStore`.
"""
