# ADR-0006 — Derivación clínica fail-closed sobre la taxonomía de handoff existente

- **Estado:** Propuesta (diseño; decisión de proceso PENDIENTE — ver riesgo)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3 (prepara Paso 8)
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §3; [`../USE-CASES.md`](../USE-CASES.md) UC-08; [`../FLOW-CATALOG.md`](../FLOW-CATALOG.md) §6; PLAN §7 "Seguridad clínica", §8 punto 6

## Contexto

Hoy el handoff:
- Solo marca `handoff_reason` y se ejecuta **después** del mensaje
  (`tools._handoff:2643`, `turn._safe_handoff:3052`).
- Normaliza el motivo libre del LLM a un catálogo **cerrado**:
  `HANDOFF_REASONS = {cliente, modelo, error, ventana, hostilidad, medicamento_no_disponible}`
  (`app/crm.py:84-86`).
- Es determinista en casos clave (hostilidad al 3er strike, G4 fraude, G9 pide
  humano, G11 promesa de escalado).

**No existe hoy** una categoría de handoff **clínico** ni una taxonomía de
consultas clínicas (diagnóstico, interacciones, embarazo/lactancia, pediatría,
reacciones adversas, síntomas de alarma). Tampoco existe un proceso humano
documentado de derivación clínica (PLAN §8 pregunta 6).

## Decisión

1. **Principio fail-closed (diseñable ya):** "fail-closed" significa, en este
   contexto, **que el sistema NO genera recomendaciones clínicas autónomas**
   (no diagnostica, no prescribe, no ajusta dosis, no sustituye tratamiento) ante
   una consulta que parezca clínica (diagnóstico, tratamiento personalizado,
   interacción, cambio de dosis). La respuesta segura por defecto es **no aconsejar
   y derivar a humano**. Esto es coherente con [ADR-0002](ADR-0002-llm-sin-autoridad.md).
2. **Alcance limitado del mecanismo actual.** Fail-closed **no implica** que la
   puerta de handoff existente esté **preparada para gestionar derivaciones
   clínicas completas**. Hoy solo marca `handoff_reason` y pausa la IA; **no**
   tiene taxonomía clínica, ni profesional responsable, ni estados de derivación,
   ni motivo `clinico`. Es suficiente para **detener** el consejo autónomo y
   escalar, pero **no** para operar un proceso clínico end-to-end.
3. **Reutilizar la puerta de handoff existente**, no crear un segundo mecanismo de
   escalado.
4. **🔸 PENDIENTE (bloquea Paso 8):** la **taxonomía clínica**, el **profesional
   responsable**, los **estados de derivación** (detectado → solicitado → asignado
   → atendido → retomado/cerrado) y si se añade un motivo `clinico` a
   `HANDOFF_REASONS` requieren **decisión de producto/clínica aprobada**. No se
   diseña contra supuestos.

## Alternativas consideradas

| Alternativa | Por qué se descarta / se aplaza |
|---|---|
| Que el bot responda consultas clínicas simples | Prohibido por PLAN §7 (no diagnosticar, no prescribir, no cambiar dosis). |
| Añadir ya el motivo `clinico` a `HANDOFF_REASONS` | Es un cambio de contrato con el CRM; requiere confirmar el catálogo real del CRM y el proceso humano (pregunta abierta). |
| Diseñar la máquina de estados clínica completa ahora | Se diseña como esqueleto marcado PENDIENTE; el detalle espera decisión. |

## Consecuencias

- **Positivas:** la postura segura (derivar, no aconsejar) se puede sostener desde
  ya con la puerta actual.
- **Negativas:** sin categoría `clinico` explícita, hoy una consulta clínica cae en
  `cliente`/`modelo`; la métrica de derivaciones clínicas no es separable. Queda
  registrado como limitación hasta el Paso 8.
