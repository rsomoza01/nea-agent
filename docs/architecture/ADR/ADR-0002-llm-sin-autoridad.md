# ADR-0002 — El LLM interpreta y redacta; no autoriza, no ejecuta, no afirma hechos

- **Estado:** Aceptada (diseño)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §2, §6; [`../POLICY-MATRIX.md`](../POLICY-MATRIX.md); PLAN §1 "Principio rector"

## Contexto

El catálogo es difuso y el LLM, sin controles, inventaba precios, equivalencias,
acciones y búsquedas basura (documentado en `app/intencion.py:1-48` y en las
guardas G1–G13). El sistema ya trata al modelo como un componente **no confiable
para hechos y permisos**: F15 decide si buscar antes de buscar, F15.1 autoriza por
término, los backstops reemplazan el texto del LLM por la lista real del catálogo
y G1–G13 vetan afirmaciones sin evidencia.

## Decisión

Formalizar como invariante de arquitectura: **el LLM interpreta lenguaje y
redacta respuestas; no concede permisos, no inventa datos de producto, no decide
que una transacción ocurrió y no ejecuta operaciones privilegiadas.**

La ruta canónica de un turno (PLAN §1) separa: comprensión → política/autorización
determinista → ejecución de caso de uso → validación de dominio → auditoría →
redacción fiel a la evidencia → envío.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Confiar en el modelo para tool-calling directo sin autorización | Es la causa raíz histórica de alucinación de precio/stock/acción. |
| LLM decide handoff/escalado por sí mismo | G9/G11/hostilidad ya son deterministas; el modelo "promete" pero no garantiza. |
| Dar al LLM el `pharmacy_id`/tenant desde el mensaje | Riesgo de cross-tenant; el tenant se valida en backend (PLAN §7 multi-farmacia). |

## Consecuencias

- **Positivas:** la frontera de seguridad es auditable y testeable sin modelo real
  (la mayoría de probes son deterministas).
- **Negativas:** cada capacidad nueva debe declarar explícitamente su evidencia y
  su guarda; más código determinista que mantener junto al prompt.
- **Invariante:** ningún caso de uso ejecuta efectos a partir de texto del LLM sin
  pasar por política (autorización) y validación (evidencia). Ver
  [ADR-0003](ADR-0003-puerta-unica-autorizacion.md) y
  [ADR-0004](ADR-0004-guards-ultima-linea.md).
