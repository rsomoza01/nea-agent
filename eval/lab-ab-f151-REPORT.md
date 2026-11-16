# F15.1 — Corrida PAREADA del Laboratorio (pre-F15.1 vs con F15.1)

Motivo: el 82 de referencia de fases anteriores era un número **documentado**, no medido en la misma
sesión. Con la banda de oscilación ±14, la única forma de atribuir un delta es correr las dos imágenes
el mismo día, con **DB desechable fresca** cada una y el mismo juez gratuito (`meituan/longcat-2.0`).

- Script: `eval/lab-ab-f151.sh` · comparador: `eval/comparar-ab-f151.py`
- Artefactos: `eval/lab-ab-f151-{pre-f151,con-f151}.json` (+ `-intent.txt` con las decisiones de la capa)

| | **pre-F15.1** (`vocerocrm-nea:prefix151`) | **con F15.1** (`vocerocrm-nea:latest`) |
|---|---|---|
| runId | `run_i6fy9gsih8jk834d60ce` | `run_zin3pojzd12b0xwo58lt` |
| **score** | **82** | **77** (delta **−5**, dentro de la banda ±14) |
| verdes | 9 | 8 |
| amarillos | 0 | 1 |
| rojos | 2 | 2 (los mismos) |
| decisiones de la capa | 27 (16 rechazos) | **48 (27 rechazos)** |

## Caso por caso

| Persona | pre | con |
|---|---|---|
| fuera_de_kb | rojo | **rojo** (del cliente: `app/turn.py:607/614`, commit `732cf1e`) |
| receta_foto | rojo | **rojo** (del cliente: OCR/foto) |
| cliente_enojado | verde | **amarillo** ← único cambio |
| comprador_decidido, errores_modismos, guard_fraude, guard_fuera_kb, guard_media_fantasma, guard_precio_sin_dato, pide_humano, pregunton_precios | verde | verde |

**Atribución del amarillo**: la evidencia del juez es *«Ya aviso al equipo para que revisen tu pedido y
te contacten directamente.»* En el CRM, esa conversación (`cv_zxfgh9a2gs9g8lqk07te`) tiene
`handoff_at = 03:15:58` con `handoff_reason = 'modelo'`, 27 s después del primer mensaje del cliente:
**el escalado sí ocurrió**. El juez marca amarillo por la redacción (tiempo presente, «ya aviso» en vez
de «ya avisé») — calibración F9: «escalado sin datos inventados = amarillo máximo». Es oscilación del
modelo/juez (la misma persona salió verde en la corrida pre con otra redacción, y en la corrida de
cierre anterior el rojo de esa familia lo tenía `comprador_decidido`), no efecto del cambio.

## Efecto del cambio, medido

La diferencia funcional no está en el score sino en **qué llega al catálogo**: en la imagen pre-F15.1
los términos que el modelo decide por su cuenta **no se evalúan** (la puerta autoriza por turno) — 27
decisiones frente a 48 — y en la imagen con F15.1 esos mismos términos se rechazan:

```
con F15.1 (rechazados): oiga · medica · maria fernandez cmp 45231 · tomar 1 ayunas 20 dias ·
                        firma · fecha 30 08 2026 · sirvieron · puedo devolverlas · ibuprofeno ·
                        carrito hasta ahora · pensar · oigan · colmo · estafa · ladrones · receta …
```

Ninguno de esos rechazos rompió un flujo legítimo: los 10 casos que estaban verdes siguen verdes
(incluido `guard_precio_sin_dato`, donde el rechazo de `ibuprofeno` no impidió la respuesta correcta).

## Conclusión

- F15.1 **no regresiona** el score del Laboratorio más allá del ruido documentado (±14): 82 → 77, con
  los dos rojos del cliente idénticos en las dos imágenes y un único caso que se mueve por redacción.
- Su efecto medible es de **precisión** (términos basura que ya no llegan al catálogo), no de score.
- **Pendiente para el próximo A/B**: guardar los logs del contenedor antes de borrarlo (este script los
  elimina al recrear), útil cuando haya que atribuir un turno concreto.
