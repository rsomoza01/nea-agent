# Diagnóstico independiente del cambio F15 — openai/gpt-6-astra-pro

## 1. ¿Resuelve las cuatro quejas?

**Parcialmente; no está demostrado de extremo a extremo.**

Con la capa encendida, las cuatro frases llegan al LLM:
- «No puede ser Indu.» y «Vivo en Ciudad Bolivar.» superan dos tokens.
- «Solo una marca?» contiene `?`.
- «Por ahroa no» supera dos tokens.

El prompt enseña explícitamente a rechazarlas como búsquedas. Eso corrige el **clasificador**, pero no demuestra que todas las búsquedas estén protegidas.

Los escenarios muestran problemas pendientes:
- «Indu» se interpreta como una persona o pierde la referencia al producto.
- Tras «Solo una marca?» aparecen alternativas sin haberse demostrado que la capa autorizara esa búsqueda.
- «en dos miligramos» no corresponde a las presentaciones mostradas.

**Evitar basura en catálogo no equivale a comprender bien la conversación.** Sin trazas de herramientas, tampoco puede afirmarse cuántas búsquedas hubo.

La evidencia debe distinguirse: se reportan **40/40**, pero el anexo muestra **49/50**, con JSON inválido. Este último es un resultado inconcluyente del clasificador, no necesariamente un falso negativo del flujo completo.

## 2. Fallos verificables y límites de evidencia

### En el código disponible

- **Fail-open, no fail-safe:** timeout, error del proveedor o JSON ilegible devuelven `None`. Según el contrato documentado, reaparece el comportamiento anterior y sus búsquedas basura.
- **JSON semánticamente inválido aceptado:** `{"motivo":"x"}` termina como negativa concluyente. `"hay_consulta":"quizás"` también. Números y listas se convierten mediante `bool()`.
- **Contrato incoherente:** admite `hay_consulta=True` sin término y actos incompatibles. El efecto depende del llamador.
- **Confianza decorativa:** no modifica decisiones; una negativa con confianza cero sigue siendo concluyente. Tampoco existe aquí la aclaración prometida.
- **Excepciones fuera del `try`:** construcción del cliente y conversión de `intent_timeout` pueden abortar el turno, sin degradación controlada.
- **Timeout inválido:** cero se sustituye por 12; negativos no se rechazan.
- **Capa apagada:** retorna antes de comprobar el vacío; incluso ese caso queda al flujo anterior.
- **Vacíos:** con capa activa, texto vacío o espacios no busca; puntuación sola llega al LLM.
- **Contexto insuficiente:** solo recibe un producto previo, no historial, opciones, pregunta pendiente ni carrito. Elipsis y cambios de producto quedan mal representados.
- **Una sola consulta:** `termino` singular no representa adecuadamente «losartán y metformina».

### Cliente y configuración

- `intent_model` incorrecto produce errores repetidos; no hay validación inicial ni circuito de corte.
- La caché usa credenciales **declaradas**, pero construye con credenciales **efectivas** heredadas. Dos configuraciones con distintos valores `openai_*` pueden reutilizar el mismo cliente equivocadamente.
- No hay cierre ni límite de caché.
- En un único event loop no hay intercalado durante su creación: no contiene `await`. Seguridad entre hilos, loops y llamadas concurrentes depende de `OpenAiLlm`, no adjunto.

### No verificable

Audios, OCR, deduplicación, aplicación del veredicto y persistencia requieren integración. Hay que comprobar que transcripción/OCR pasan por el mismo control y que un OCR vacío no activa rutas alternativas.

## 3. ¿Solo protege el backstop?

**Esta implementación no controla herramientas:** únicamente devuelve un veredicto. No contiene autorización ni interceptación de búsquedas iniciadas por el chat.

Si la integración solo consulta `entender()` desde el backstop, **el agujero es real**. Si existe otro control central, falta verlo. No aceptaría afirmar cobertura completa sin inspeccionarlo.

No prohibiría toda búsqueda tras un acto conversacional: preguntar por otra marca puede justificar consultar alternativas. Exigiría que cada búsqueda responda a una necesidad autorizada del turno, no que coincida ciegamente con una etiqueta.

## 4. Coste y latencia

El camino rápido abarata una clasificación **sin evidencia positiva suficiente**. Con candidato, `delivery` o `transferencia` cumplen sus condiciones aunque sean conversación. La reproducción completa depende del extractor omitido.

Sí, `_PALABRAS_CONVERSACION` es una **lista de exclusión del atajo**. No bloquea medicamentos directamente, pero define qué se acepta sin comprender: conserva el defecto estructural rechazado por producto.

Saludos conocidos requieren una llamada; puede ser razonable si se rechazan reglas léxicas. Más importante: una clasificación separada puede duplicar trabajo antes del chat.

Si vence siempre el timeout, se añaden aproximadamente **12 segundos por turno no rápido**, más el flujo anterior. Cancelar localmente no garantiza cancelar facturación remota. Los 104,5 segundos del corpus no permiten calcular latencia por llamada ni coste.

## 5. Tests y falsos negativos

Los 144 tests verdes no son evaluables sin su código. Los ejemplos del prompt reaparecen en la evaluación: sirven como regresión, no como prueba suficiente de generalización.

Faltan, prioritariamente:
- Integración con **espía del catálogo**, incluyendo llamadas iniciadas por el chat.
- Timeout, constructor fallido, configuración inválida y JSON semánticamente incorrecto.
- Dos configuraciones con credenciales heredadas distintas.
- Audio/OCR, negaciones mixtas, consultas múltiples y contexto elíptico.
- Corpus independiente con repeticiones, no una ejecución única.

Consultas legítimas vulnerables:
- «¿Y de 50?», «la segunda», «otra marca».
- «No ese; necesito losartán».
- «Shampoo para caspa»: el sistema habla principalmente de medicamentos.
- «No me sirvió, ¿tienen otra cosa?»: mezcla reclamo y nueva consulta.

Una clasificación falsa negativa no garantiza que el chat pregunte: eso debe implementarse.

## 6. Arquitectura y auditoría

**Entender antes de buscar es correcto; distribuir la autoridad entre heurístico, clasificador y herramientas no lo es.**

Preferiría una decisión estructurada por turno —consultas, restricciones, referencias y necesidad de aclaración— seguida de un ejecutor central que autorice búsquedas. El chat puede proponer acciones, no saltarse ese ejecutor.

No se puede evaluar la migración 014 sin verla. Además, columnas solo en `med_queries` son insuficientes: **los rechazos sin búsqueda también deben quedar registrados**.

Hace falta un evento por decisión, enlazado con turno y búsquedas: versión del prompt/modelo, fuente, resultado, error, latencia, término original/final y origen de la llamada. Eso permite investigar; atribuir automáticamente “comprensión versus catálogo” exige también resultados y evaluación de corrección.

## Veredicto y cambios prioritarios

**Condicional: dirección correcta, implementación insuficiente para declarar resuelto F15. Nota: 5/10.**

1. **Integración del turno y dispatcher de herramientas —anexos pendientes—:** centralizar autorización para *todas* las búsquedas; ante incertidumbre o fallo, aclarar en vez de recuperar el heurístico. Evita el bypass y la regresión.
2. **`app/intencion.py::_veredicto_desde_json`:** validar esquema estricto: booleano real, acto válido, confianza finita, término obligatorio para consulta y coherencia entre campos. Rechazar contratos inválidos como inconcluyentes.
3. **`entender` / `cliente_intencion`:** proteger construcción y parsing; validar timeout positivo; usar configuración efectiva en la clave; gestionar cierre de clientes y circuito de corte.
4. **`camino_rapido`:** retirar la aceptación por forma/lista. Sustituirla únicamente por identificación inequívoca respaldada por catálogo/contexto; lo ambiguo va al decisor.
5. **`_contexto` / contrato:** incorporar estado conversacional compacto y múltiples consultas; representar explícitamente aclaraciones y restricciones.
6. **Migración 014, persistencia y tests:** registrar decisiones sin búsqueda y añadir las pruebas anteriores con conteo real de invocaciones.

Para aprobar el cambio necesito los anexos de integración, migración y tests; no basta con las respuestas visibles del agente.
