# Prompts de ejecución — modernización arquitectónica NEA

Documento complementario de `PLAN-MODERNIZACION-FARMACIA.md`.

## Instrucciones generales

- Repositorio: `arquimedesgarcia/genteFarma-nea`.
- Rama de referencia auditada: `agent-qa`.
- Rama documental que contiene este plan: `docs/plan-modernizacion-farmacia`.
- El plan fue preparado contra `agent-qa` en SHA `9715eeea4b6a8870bbda219cc4d35996be22a4b3`. Verifica siempre el HEAD real antes de trabajar.
- Trabaja en el repositorio local del usuario. No supongas que tienes acceso a una copia local si no puedes comprobarla.
- Una etapa por sesión/PR. No ejecutes los prompts siguientes anticipadamente.
- No hagas push, merge, rebase de ramas compartidas ni despliegues.
- No alteres `agent-qa`, `desarrollo`, `main`, `master` ni ramas del upstream.
- No mezcles esta modernización con el PR de integración upstream ni con features no relacionadas.
- Si falta acceso, entorno, credenciales o evidencia, reporta BLOCKED; no inventes resultados.
- El propietario revisa y autoriza cada etapa antes de continuar.

### Prompt general que debe preceder a cada etapa

```text
Actúa como arquitecto de software y/o ingeniero senior responsable de esta etapa del proyecto NEA, un agente WhatsApp especializado en farmacia.

Lee primero:
- docs/architecture/PLAN-MODERNIZACION-FARMACIA.md
- docs/architecture/PROMPTS-MODERNIZACION.md
- Los documentos y pruebas existentes que correspondan al alcance.

Respeta estrictamente el alcance del prompt de etapa. Verifica HEAD, rama y árbol de trabajo. No supongas que el código coincide con documentación antigua.

Reglas no negociables:
1. No debilites ni dupliques F14, F15, F15.1, G5, G6, G13 ni ningún guard vigente.
2. La única puerta de autorización de búsqueda por término existente debe seguir siendo tools._autorizar_busqueda. No crees bypass ni segunda puerta.
3. El LLM no autoriza operaciones, no decide permisos, no escribe SQL ni afirma transacciones que no ocurrieron.
4. No cambies contratos públicos o comportamiento observable sin pruebas y justificación.
5. No reescribas app/turn.py ni app/tools.py de golpe.
6. No alteres sondas, fixtures ni tests existentes para conseguir un PASS.
7. No añadas dependencias ni cambies esquema de DB sin autorización, ADR y plan de rollback.
8. No hagas push, merge, rebase de ramas compartidas ni despliegues.
9. No declares ejecutado lo que no ejecutaste. Usa PASS, FAIL, BLOCKED o NOT RUN.
10. Si una protección debe modificarse, detente y presenta una solicitud de autorización específica.

Al terminar entrega:
- resumen ejecutivo;
- rama, HEAD inicial/final y archivos modificados;
- decisiones y justificación;
- pruebas ejecutadas con comandos y resultados;
- pruebas no ejecutadas y motivo;
- riesgos y rollback;
- lista de preguntas/decisiones pendientes;
- recomendación de continuar o detenerse.

No avances a la siguiente etapa por iniciativa propia.
```

---

## Prompt 01 — Congelar y medir la línea base

**Rama:** `chore/architecture-baseline`  
**Modo:** auditoría y ejecución de pruebas; no modificar runtime.

```text
Ejecuta únicamente el Paso 1 del plan de modernización: congelar y medir la línea base actual.

1. Verifica el repositorio, rama, HEAD, remotos y árbol de trabajo. La referencia esperada es agent-qa, pero confirma el SHA real. No cambies de rama ni actualices refs compartidas.
2. Lee docs/CIERRE-VERSION-2026-11-09.md, docs/VALIDACION-INTEGRACION-AUTOR-QA.md y los specs F14/F15.1/G5/G13 pertinentes.
3. Identifica el intérprete/venv correcto. No instales ni actualices paquetes.
4. Ejecuta, si el entorno está disponible:
   - python -m py_compile app/*.py
   - python -m pytest tests/ -q
   - sondas deterministas P1/P2/P3;
   - sonda F14;
   - probe_autorizacion_por_termino.py;
   - pruebas/sondas G5, G6 y G13 disponibles.
5. Localiza scripts y comandos reales antes de ejecutarlos. No inventes rutas ni cambies los instrumentos.
6. Distingue pruebas deterministas, pruebas que requieren modelo real, Docker, DB, CRM o secretos.
7. No intentes reparar fallos en esta etapa. Registra baseline y hallazgos por separado.
8. Crea docs/architecture/BASELINE.md y docs/architecture/BASELINE-TEST-MATRIX.md con SHA, fecha, entorno, comandos, resultado y limitaciones.
9. Si el árbol no está limpio, no sobrescribas trabajo del usuario. Detente y reporta.
10. No hagas commit ni push salvo autorización explícita; deja los documentos preparados y presenta diff.

Criterio de salida: línea base reproducible o bloqueos explícitos, sin cambios de comportamiento.
```

## Prompt 02 — Arquitectura AS-IS y mapa de dependencias

**Rama:** `docs/architecture-as-is`  
**Modo:** análisis y documentación; no modificar runtime.

```text
Ejecuta únicamente el Paso 2. Audita el código actual y documenta cómo funciona realmente NEA.

Traza con símbolos y líneas:
1. Webhook de entrada → extracción/normalización → coalescing → handle_flush → run_turn → tool loop → ejecución de herramientas → guards → envío.
2. Ciclo de vida y dependencias de AppContext, Store, PgStore/MemoryStore, ToolRuntime, CrmClient, Llm/OpenAiLlm.
3. Todos los módulos y workers relacionados con mensajes, estado, follow-up, relay, media y observabilidad.
4. app/turn.py: agrupa funciones por responsabilidad y llama a sus dependencias. Identifica IO, reglas de dominio, estado, presentación y helpers puros.
5. app/tools.py: inventaría herramientas y operaciones, permisos, efectos secundarios, fuentes de datos y validaciones.
6. Catálogo, relevancia, aliases, precio, disponibilidad, carrito, pedido, seguimiento, OCR/media y handoff.
7. Persistencia: entidades/tablas, estados, transacciones, locks, índices relevantes y límites de tenant/farmacia. No inferir esquema: inspecciona migraciones/SQL/modelos reales.
8. Clasifica cada capacidad como Customer, Staff, transversal o legado/agenda. Si no puede determinarse, marca pendiente.
9. Identifica riesgos de dependencias circulares, acoplamiento y puntos de entrada alternativos.

Crea:
- docs/architecture/AS-IS.md
- docs/architecture/DEPENDENCY-MAP.md
- docs/architecture/TOOL-INVENTORY.md
- docs/architecture/FLOW-CATALOG.md
- docs/architecture/FLOW-ORDERS.md
- diagramas Mermaid de secuencia y componentes.

Cada afirmación importante debe apuntar a ruta, símbolo y líneas aproximadas. No propongas todavía mover módulos ni hagas refactor. Contrasta los documentos con los tests existentes.
```

## Prompt 03 — Dominio farmacéutico y diseño TO-BE

**Rama:** `docs/pharmacy-domain-design`  
**Modo:** diseño; no modificar runtime.

```text
Ejecuta únicamente el Paso 3. A partir del AS-IS y el mapa de dependencias aprobados, diseña la arquitectura objetivo.

Define:
1. Actores y límites: cliente, empleado, farmacéutico, administrador, farmacia/tenant y sistema. No inventes roles que no existan; registra brechas.
2. Casos de uso Customer: buscar catálogo, consultar detalle/precio, consultar disponibilidad, carrito, preparar pedido, confirmar pedido y seguimiento.
3. Flujo de consulta clínica y handoff humano.
4. Contratos tipados de entrada/salida, errores y resultados: encontrado, no encontrado, ambiguo, no autorizado, parcial, proveedor no disponible, requiere confirmación y derivado.
5. Matriz de políticas: actor × acción × recurso × farmacia × precondición × auditoría.
6. Evidencia necesaria para precio, stock, composición, equivalencia, presentación y estado de pedido.
7. Máquina de estados del carrito/pedido basada en el CRM real.
8. Reglas de idempotencia, concurrencia, expiración y recuperación.
9. Separación entre comprensión de intención, autorización, ejecución de caso de uso, validación y redacción.
10. Arquitectura modular objetivo y plan de transición incremental, conservando el monolito.
11. Mapeo explícito entre F14/F15/F15.1 y guards existentes con los componentes futuros. No crear otra puerta.
12. ADR para decisiones con alternativas y consecuencias.

Crea:
- docs/architecture/TO-BE.md
- docs/architecture/DOMAIN-MODEL.md
- docs/architecture/USE-CASES.md
- docs/architecture/POLICY-MATRIX.md
- docs/architecture/MIGRATION-ROADMAP.md
- docs/architecture/ADR/ADR-*.md

No codifiques ni crees carpetas de aplicación vacías. Marca cada supuesto y decisión pendiente. Presenta el diseño para aprobación antes del Paso 4.
```

## Prompt 04 — Contratos de aplicación y puertos mínimos

**Rama:** `refactor/application-contracts`  
**Modo:** implementación limitada, aditiva.

```text
Ejecuta únicamente el Paso 4, y solo si los pasos 1–3 fueron aprobados.

Objetivo: introducir contratos explícitos de aplicación sin cambiar el comportamiento del bot.

1. Verifica que los documentos AS-IS/TO-BE reflejen el HEAD actual.
2. Selecciona un único caso de uso inicial (preferiblemente búsqueda de catálogo) y define el contrato mínimo necesario.
3. Introduce DTOs/resultados tipados y errores de aplicación solo donde reduzcan ambigüedad real.
4. Define puertos mínimos para dependencias externas que el caso de uso realmente necesita. No abstraigas cada función por anticipado.
5. Adapta los componentes actuales mediante wrappers/adaptadores compatibles.
6. No dupliques CrmClient, Store, ToolRuntime ni clientes LLM.
7. No muevas grandes bloques de turn.py/tools.py en esta etapa.
8. Añade pruebas unitarias y de contrato para éxito, no encontrado, parcial, error externo y cancelación/timeouts pertinentes.
9. Conserva serialización, mensajes y efectos observables.
10. Ejecuta suite y sondas afectadas; compara el comportamiento antes/después.
11. Documenta cualquier dependencia que impida introducir el puerto sin refactor mayor.

No hagas migraciones DB, cambios de permisos, cambios de prompt ni nuevas dependencias. El diff debe ser pequeño y justificable.
```

## Prompt 05 — Extraer caso de uso de búsqueda de catálogo

**Rama:** `refactor/pharmacy-catalog-usecase`  
**Modo:** primera extracción funcional.

```text
Ejecuta únicamente el Paso 5. Extrae la búsqueda de catálogo como caso de uso de aplicación, preservando el comportamiento actual.

Antes de editar:
1. Lee la implementación completa de búsqueda en tools.py y sus helpers en aliases.py/relevancia.py/turn.py.
2. Identifica cada llamada de catálogo, normalización, typo correction, relevancia, forma/presentación, laboratorio y filtrado.
3. Identifica todas las puertas de autorización. La puerta única de búsqueda por término sigue siendo tools._autorizar_busqueda.
4. Determina qué datos de CRM constituyen evidencia y cuáles no.

Implementación:
1. Extrae solo la responsabilidad acordada en TO-BE, con inputs/output explícitos.
2. Reutiliza algoritmos existentes; no reescribas el buscador ni cambies heurísticas.
3. El adaptador de herramienta debe llamar una sola vez al caso de uso.
4. El caso de uso no debe llamar al LLM ni tomar decisiones de autorización.
5. Devuelve resultados estructurados con fuente, campos disponibles, estado y limitaciones.
6. No afirmes composición/equivalencia sin evidencia del producto.
7. Conserva orden, deduplicación, límites, filtros y formato externo actual.
8. No cambies F14/F15/F15.1 ni guards.

Pruebas:
- unitarias de normalización y selección;
- contrato del catálogo;
- regresión de F14;
- autorización F15.1 por término;
- casos de búsqueda ambigua, typo, varios términos, presentación y resultado vacío;
- suite completa.

Compara salida anterior/nueva para corpus existente. Si hay diferencias no intencionales, corrige o detén. No edites sondas para hacerlas pasar.
```

## Prompt 06 — Modularizar orquestación de turn.py incrementalmente

**Rama:** `refactor/turn-orchestrator`  
**Modo:** refactor controlado.

```text
Ejecuta únicamente el Paso 6. No reescribas app/turn.py.

1. Usa DEPENDENCY-MAP.md para escoger una sola responsabilidad de bajo riesgo y delimitar entradas/salidas.
2. Identifica consumidores, efectos secundarios, estado mutado, excepciones y orden de ejecución.
3. Prefiere extraer primero funciones puras y luego un servicio/coordinador pequeño.
4. Mantén run_turn y el flujo público como fachada compatible mientras sea útil.
5. No alteres el orden de autorización, tools, guards, envío ni handoff.
6. Preserva conversation_lock, coalescing, idempotencia, tool rounds, backstops y fallbacks.
7. No crees un nuevo módulo central que absorba todas las reglas.
8. Añade tests unitarios y de integración para la responsabilidad extraída.
9. Ejecuta sondas P1/P2/P3, F14, F15.1, guards relevantes y suite completa.
10. Presenta métricas concretas de reducción de acoplamiento (dependencias/imports/LOC trasladadas) sin usar LOC como único éxito.

Si la extracción exige modificar una barrera protegida o cambia un comportamiento del cliente, detente y solicita autorización con un spec separado.
```

## Prompt 07 — Carrito y flujo transaccional de pedido

**Rama:** `feat/pharmacy-order-workflow`  
**Modo:** implementación de dominio; requiere diseño aprobado.

```text
Ejecuta únicamente el Paso 7. Primero audita el modelo real de carrito/pedido y las APIs CRM; no supongas que existe una entidad de pedido persistida.

Diseña e implementa de forma incremental:
1. Distingue carrito local, pedido provisional, confirmación del cliente y pedido aceptado por la farmacia.
2. Define estados y transiciones permitidas con base en el sistema fuente.
3. Revalida precio/disponibilidad al confirmar cuando la API lo permita.
4. Introduce idempotency key estable por operación y protección contra webhooks duplicados/reintentos.
5. Protege mutaciones concurrentes con transacciones/locks adecuados al almacenamiento actual.
6. Nunca informes pedido confirmado/registrado si el CRM no lo confirmó.
7. Maneja timeout ambiguo consultando estado antes de repetir una mutación.
8. Conserva cantidades, presentaciones, moneda y totales respaldados.
9. Define expiración y recuperación sin borrar pedidos ya confirmados.
10. Incluye auditoría con actor, farmacia, operación, correlación y resultado.

Pruebas obligatorias:
- confirmación explícita y rechazo;
- doble mensaje/webhook;
- timeout antes/después de respuesta del CRM;
- dos operaciones concurrentes;
- precio o stock cambió;
- CRM no disponible;
- recuperación de estado y rollback.

No hagas migración sin ADR, backup/rollback y prueba en DB desechable. No mezcles Staff Agent.
```

## Prompt 08 — Seguridad clínica y derivación humana

**Rama:** `feat/clinical-handoff`  
**Modo:** implementación de seguridad.

```text
Ejecuta únicamente el Paso 8. El objetivo es derivar de forma segura, no construir un médico virtual.

1. Confirma con la documentación del producto y responsables farmacéuticos qué categorías se derivan.
2. Define clasificación conservadora para diagnóstico, síntomas de alarma, dosis, interacciones, contraindicaciones, embarazo/lactancia, pediatría, reacciones adversas y sustituciones individualizadas.
3. No generes diagnóstico, prescripción ni cambios de tratamiento.
4. Implementa caso de uso de handoff con estados reales y responsables disponibles.
5. Evita duplicar handoffs y registra correlación/estado.
6. Mientras espera humano, limita al bot a mensajes de recepción/estado autorizados.
7. Define timeout, reintento, escalamiento y cierre según operación real.
8. No incluyas información clínica innecesaria en logs.
9. Mantén los guards actuales y añade pruebas específicas sin relajarlos.
10. Prueba derivación, fallo de CRM, reintento, doble solicitud, atención y reanudación de conversación.

No inventes un protocolo clínico: cualquier regla no aprobada queda como decisión pendiente y se mantiene fail-safe.
```

## Prompt 09 — Identidad, permisos y frontera Customer/Staff

**Rama:** `feat/pharmacy-actor-policies`  
**Modo:** seguridad; requiere modelo de identidad aprobado.

```text
Ejecuta únicamente el Paso 9.

1. Audita cómo se identifican hoy clientes, operadores y personal. No confundas número de WhatsApp con identidad autenticada de empleado.
2. Documenta los mecanismos reales de autenticación, roles, farmacia/tenant y sesiones.
3. Diseña autorización servidor-side por actor, acción, recurso y farmacia.
4. Aplica tenant scope en cada repositorio/servicio, no solo en prompts o filtros de UI.
5. Deniega por defecto identidad desconocida, rol ausente, farmacia no coincidente y contexto ambiguo.
6. Reutiliza los casos de uso de dominio; Customer y Staff deben tener políticas distintas, no lógica de negocio duplicada.
7. Toda mutación Staff debe registrar actor, farmacia, acción, resultado y correlation_id.
8. Protege contra IDOR, cambio de tenant, spoofing de rol y replay.
9. Añade pruebas negativas: cliente invoca operación Staff, empleado de farmacia A opera en B, rol insuficiente, identidad expirada.
10. No actives funciones Staff nuevas ni cambies canales de autenticación sin aprobación explícita.

Si el producto no dispone de identidad verificable para empleados, entrega diseño y bloqueo; no simules autenticación.
```

## Prompt 10 — Fronteras de infraestructura, resiliencia y observabilidad

**Rama:** `refactor/infrastructure-boundaries`  
**Modo:** robustez incremental.

```text
Ejecuta únicamente el Paso 10.

Audita adaptadores actuales de PostgreSQL, CRM, WhatsApp, LLM, media y workers.

1. Define contratos de timeout y errores por dependencia.
2. Revisa reintentos: nunca repitas una mutación no idempotente a ciegas.
3. Añade backoff/circuit breaker solo si existe caso medido; evita complejidad innecesaria.
4. Propaga correlation_id/turn_id y pharmacy_id en logs/trazas.
5. No registres tokens, API keys, payloads completos, diagnósticos ni datos personales innecesarios.
6. Añade métricas útiles: latencia por dependencia, error rate, turnos sin respuesta, autorización de búsqueda, resultados parciales, handoff y pedidos.
7. Distingue fallo de proveedor de no encontrado y de no autorizado.
8. Documenta modo degradado y recuperación.
9. Prueba timeouts, conexión caída, respuestas malformadas, reintento e idempotencia.
10. Mantén observabilidad no-op cuando no haya proveedor de métricas configurado.

No introduzcas plataformas nuevas ni dependencias sin ADR y medición de coste/beneficio.
```

## Prompt 11 — Staff Agent (solo después de seguridad aprobada)

**Rama:** `feat/pharmacy-staff-agent`  
**Modo:** nueva superficie funcional; requiere pasos 7–10 según alcance.

```text
Ejecuta únicamente el Paso 11. No empieces si la identidad/permisos Staff no están implementados y probados.

1. Lista operaciones internas autorizadas por el negocio y clasifícalas por riesgo.
2. Reutiliza casos de uso de catálogo, inventario y pedidos; no dupliques lógica Customer.
3. Cada operación debe comprobar actor, rol, farmacia, recurso y precondiciones en servidor.
4. Las mutaciones requieren confirmación explícita cuando el riesgo lo amerite.
5. No permitas que el LLM construya SQL ni invoque operaciones arbitrarias.
6. Registra auditoría de todas las consultas sensibles y mutaciones.
7. Implementa separación de herramientas Customer/Staff y schemas mínimos por rol.
8. Prueba escalamiento de privilegios, tenant crossing, identidad no autenticada, replay y operaciones no permitidas.
9. No agregues acceso a datos clínicos de pacientes salvo autorización legal/operativa específica y controles reforzados.
10. Entrega un piloto cerrado; no habilites a todos los usuarios por defecto.
```

## Prompt 12 — Consolidación, regresión y readiness de producción

**Rama:** `release/pharmacy-architecture-v1`  
**Modo:** QA/revisión; no desplegar.

```text
Ejecuta únicamente el Paso 12. Actúa como QA independiente y revisor de release, no como autor de los cambios.

1. Verifica la cadena de PRs, aprobaciones, SHAs y decisiones ADR.
2. Ejecuta suite completa, py_compile, pip check y auditoría de dependencias si aplica.
3. Ejecuta regresiones F14/F15/F15.1, G5/G6/G13 y sondas P1/P2/P3.
4. Ejecuta integración con DB desechable y CRM sandbox; no uses datos de clientes reales.
5. Prueba E2E de: consulta de producto, precio, disponibilidad, carrito, confirmación, seguimiento y handoff clínico.
6. Prueba aislamiento entre farmacias y permisos Staff si Staff forma parte del release.
7. Prueba concurrencia, idempotencia, timeouts, reintentos, mensajes duplicados y recuperación.
8. Revisa logs por secretos/PII, trazabilidad, métricas y alertas.
9. Verifica migraciones, backup, restore y rollback.
10. Revisa cambios no documentados, dependencias nuevas y compatibilidad hacia atrás.
11. No corrijas código durante QA; abre hallazgos reproducibles con severidad.
12. Emite matriz PASS/FAIL/BLOCKED/NOT RUN y recomendación GO/NO-GO.

No hagas deploy, merge ni cambies flags de producción. La aprobación final corresponde al propietario.
```

---

## Plantilla para el agente QA independiente después de cada PR

```text
Actúa como QA independiente. No implementes correcciones.

Valida el PR/branch indicado contra el plan y el alcance aprobado:
1. Confirma base, HEAD, diff y archivos tocados.
2. Comprueba que no se alteraron barreras fuera del alcance.
3. Ejecuta pruebas nuevas, suite completa y regresiones aplicables.
4. Reproduce casos límite y negativos.
5. Verifica compatibilidad y efectos secundarios.
6. Revisa seguridad, aislamiento de farmacia, idempotencia y datos falsos según aplique.
7. Clasifica cada criterio PASS/FAIL/BLOCKED/NOT RUN con evidencia.
8. Reporta defectos con archivo, línea, pasos de reproducción y severidad.
9. No modifiques código, tests, fixtures ni sondas.
10. No hagas merge ni push.

Entrega docs/architecture/PR-<branch>-QA.md y una recomendación APPROVE / REQUEST CHANGES / BLOCKED. No avances a otra etapa.
```
