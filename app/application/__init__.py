"""Capa de aplicación de NEA (Paso 4 — contratos y puertos mínimos).

Introduce contratos tipados y puertos para casos de uso de farmacia **sin cambiar
el comportamiento del bot**. El primer caso de uso es la búsqueda de catálogo
(UC-01). Esta capa es **aditiva y paralela**: hoy no está cableada en la ruta
caliente (`turn.py`/`tools.py`); el cableado funcional corresponde al Paso 5.

Invariante (ADR-0002): la aplicación **no autoriza** búsquedas (eso sigue siendo la
puerta única `tools._autorizar_busqueda`, F15.1) ni llama al LLM; solo orquesta un
caso de uso ya existente y traduce su resultado a un contrato tipado.
"""
