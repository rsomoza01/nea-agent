# Integración selectiva del autor original — GenteFarma NEA

**Estado:** PARCIAL — cambios portados a rama de trabajo; validación local obligatoria pendiente.  
**Destino previsto:** `agent-qa`  
**Rama de trabajo:** `fix/integracion-autor-compatible`  
**Base verificada:** `0f09e0a3f7fb60a0e49fbd4afa5d24b28e75bdbb`  
**Upstream:** `kevinrivm/nea-agent`  
**HEAD upstream/main:** `ad891389cc5402e35a9b2a3b4dcd15fda0a846d5`

## 1. Alcance y reglas respetadas

Se leyó `docs/PROMPT-CODIFICADOR-INTEGRAR-AUTOR-ORIGINAL.md` desde el HEAD real de `agent-qa`.

El prompt nombra `cee0092` como HEAD, pero la rama actual está en `0f09e0a`. Se tomó el HEAD real. La skill local `vocero-nea-qa` no está disponible en esta sesión y tampoco existe acceso al checkout local, proxy `:11500`, Docker Desktop o DB desechable. Por eso se hizo curaduría y portación remota estática, pero **no se simula ni declara validación runtime**.

No se modificaron:
- `app/tools.py`, `app/turn.py`, `app/guards.py`, `app/intencion.py`, `app/relevancia.py`;
- F14, F15, F15.1, las puertas de autorización por término/candidato ni G1-G12;
- ramas `agent-qa`, `desarrollo`, `main` del espejo;
- ningún ref del upstream.

Los cambios quedaron en una rama separada porque el prompt exige dejar el resultado en `agent-qa` solo con todos los niveles de validación verdes.

## 2. Línea base y delta upstream

| Referencia | SHA | Verificación |
|---|---|---|
| `agent-qa` | `0f09e0a3f7fb60a0e49fbd4afa5d24b28e75bdbb` | HEAD consultado |
| Prompt | blob `714eeae4083a505d93d5ac7a5b0e172a968ee97d` | leído desde agent-qa |
| `upstream/main` | `ad891389cc5402e35a9b2a3b4dcd15fda0a846d5` | HEAD consultado |
| Base upstream histórica | `249d05a78bc114fcef16e8ad8160abda362782ec` | parent de main en la integración #56 |

La comparación `249d05a...ad89138` muestra diez commits en el delta de upstream. Los archivos modificados por el delta agregado son:
- `.github/workflows/imagen-dev.yml`
- `CHANGELOG.md`
- `CLAUDE.md`
- `Dockerfile.deploy`
- `app/ensayo.py` (nuevo)
- `app/main.py`
- `app/multiorg.py`
- `requirements.txt`
- `tests/test_ensayo.py` (nuevo)

Commits del delta:
- `4f110da`: optimización de razonamiento y ruteo por rendimiento.
- `7d0a0c8`: merge de optimización.
- `47c61d4`: sincronización de dev con main.
- `5ffc420` y `8186fb8`: CI e imagen de desarrollo para Coolify.
- `fb8bdf5` y `38bee53`: modo de ensayo Vocero Cloud.
- `4a473c7` y `83f2d3e`: actualización de seguridad pypdf 6.21.2 → 6.21.4.
- `ad89138`: merge de producción.

## 3. Inventario de ramas upstream

Se listaron 50 ramas publicadas por upstream. Las diez ramas que se compararon explícitamente contra el HEAD de main (`ad89138`) —`perf/velocidad-del-agente`, `perf/velocidad-a-main`, `feat/modo-ensayo`, `fix/pypdf-6.21.4`, `fix/audit-hardening`, `llm-proveedor-configurable`, `feat/blindaje-extraccion`, `fix/texto-junto-a-herramientas`, `fix/turno-sin-crm-y-promesas` y `fix/turnos-serializados`— no tienen commits por delante de main (`ahead_by=0`). Son historial ya incorporado o ramas antiguas, no candidatos adicionales a portar desde esos refs.

Nombres de las 50 ramas inventariadas:
`208-agenda-nea-recordatorios`, `209-nea-configuracion-guiada`, `arreglo-arranque-multiorg`, `arreglo-respuesta-cerebro`, `ci/dev-en-aisha`, `ci/imagen-ghcr`, `codex/arnes`, `dev`, `docs/cierre-1.0.0`, `docs/cierre-1.1.0`, `docs/release-1.0.0`, `feat/agenda-sin-ambiguedad`, `feat/blindaje-extraccion`, `feat/cierre-sin-rumbo`, `feat/modo-ensayo`, `feat/olvidar-conversacion`, `feat/webhook-url-secreta`, `feat/031-nea-por-despacho`, `feat/033-laboratorio-por-contrato`, `feat/034-roles-como-datos`, `feat/038-mover-etapa`, `fix/audit-hardening`, `fix/calendario-y-horas-libres`, `fix/cita-cancelada-sin-inventar`, `fix/cloud-media-route`, `fix/compatibilidad-agenda-vocero`, `fix/conflicto-409-anidado`, `fix/contexto-cita-cerebro`, `fix/dedup-por-mensaje`, `fix/fecha-solo-si-la-pide`, `fix/huecos-por-fecha`, `fix/lista-de-testers`, `fix/modelo-que-oye`, `fix/pypdf-6.21.4`, `fix/relay-modo-estandar`, `fix/texto-junto-a-herramientas`, `fix/turno-sin-crm-y-promesas`, `fix/turnos-serializados`, `fix/whatsapp-formato-candado-health`, `fix/zona-del-negocio`, `fix/zoom-reminder-confirmation`, `fix/032-seguimiento-permitidos`, `llm-proveedor-configurable`, `main`, `modo-cloud-vocero-multitenant`, `motivo-sin-credencial`, `multiorg-un-proceso-muchos-negocios`, `perf/velocidad-a-main`, `perf/velocidad-del-agente`, `readme-una-nea-por-negocio`.

## 4. Tabla de decisión

| Commit/rama del autor | Qué hace | ¿Ya lo tenemos? | Decisión | Riesgo | Evidencia |
|---|---|---|---|---|---|
| `4f110da` — rendimiento LLM | Envía `reasoning.effort` y `provider.sort` por `extra_body`; si el proveedor los rechaza, reintenta sin ellos | No | **Adaptar** | Medio | El cliente de farmacia no tenía estos ajustes. Se portaron como opciones explícitas, apagadas por defecto. No se importó el default del autor ni su configuración de proveedores. |
| `fb8bdf5` — modo ensayo | Endpoint Vocero Cloud con ficha firmada, CRM de solo lectura y memoria efímera | No | **Descartar** | Alto | Depende de `crm_brains`, despacho, `multiorg`, firma de Vocero Cloud y contratos que no existen en esta aplicación de farmacia mono-`provider_id`. |
| `4a473c7` — pypdf 6.21.4 | Corrige siete CVE identificadas por el job de seguridad del autor | Parcial | **Integrar versión de dependencia** | Medio | `app/media.py` importa `PdfReader`; `requirements.txt` aceptaba `pypdf>=5.1`, por lo que podía instalar una versión vulnerable. Se fija `pypdf==6.21.4`, la versión corregida usada por upstream. Falta correr `pip-audit` y tests de PDF. |
| `5ffc420` / `8186fb8` — CI Coolify | Imagen de desarrollo y despliegue a entorno Coolify de Aisha | No | **Descartar** | Bajo | Pipeline y secretos específicos del entorno upstream; no corresponde al despliegue de farmacia. |
| `Dockerfile.deploy` | Imagen del flujo de desarrollo de Vocero Cloud | No | **Descartar** | Medio | No reemplaza ni complementa automáticamente el Dockerfile del despliegue actual. |
| `app/main.py` / `app/multiorg.py` del modo ensayo | Cableado de endpoint y resolución multi-organización | No | **Descartar** | Alto | No portar archivos completos ni introducir multi-tenancy como efecto colateral de un modo de prueba. |
| Documentación upstream | Documenta el contrato Vocero Cloud y el entorno de despliegue | Parcial/no aplicable | **Descartar** | Bajo | No aporta comportamiento de farmacia y puede confundir el contrato del proyecto. |

## 5. Cambios portados en esta rama

### 5.1 Seguridad de dependencia pypdf

- `requirements.txt`: `pypdf>=5.1` → `pypdf==6.21.4`.
- No se cambió el resto de dependencias ni se adoptó el pinning global del upstream.
- El consumidor es `PdfReader` en `app/media.py`.
- Pendiente: instalar dependencias, ejecutar `pip-audit`, pruebas de lectura PDF y suite completa.

### 5.2 Hints de rendimiento LLM, opt-in

Archivos:
- `app/config.py`: agrega `llm_reasoning_effort` y `llm_provider_sort`, ambos vacíos por defecto.
- `app/llm.py`: acepta los parámetros opcionales; los envía por `extra_body`; si el error menciona que el proveedor no admite `reasoning` o `provider`, desactiva los hints para esa instancia y reintenta usando el mecanismo de reintentos existente.
- `app/main.py`: conecta la configuración con `OpenAiLlm`.
- `.env.example`: documenta `LLM_REASONING_EFFORT` y `LLM_PROVIDER_SORT` comentados y apagados.
- `tests/test_llm_provider_hints.py`: agrega cuatro pruebas para defaults, payload, rechazo del proveedor y error no relacionado.

**No se activan los hints en producción.** La mejora de latencia del autor fue medida con otro modelo y otro entorno. En esta farmacia debe realizarse A/B con el proveedor y modelo reales antes de habilitar `minimal` o `throughput`. No se añade ningún tope de tokens.

### 5.3 Límites de seguridad

No se tocó la lógica farmacéutica. F14/F15/F15.1 y G1-G12 quedan intactos en el diff de esta rama. Tampoco se copió el modo ensayo ni la arquitectura multi-organización.

## 6. Validación y evidencia

| Validación requerida | Estado | Observación |
|---|---|---|
| Inventario upstream y delta de main | **HECHO** | 50 ramas listadas; delta main de 10 commits y archivos identificados |
| Curaduría de candidatos | **HECHO** | Tabla de decisión anterior |
| Revisión remota del diff | **HECHO** | 7 commits delante de agent-qa; 6 archivos afectados |
| `py_compile app/*.py` | **NO EJECUTADO** | Sin checkout local en esta sesión |
| `.venv-test/Scripts/python.exe -m pytest tests/ -q` | **NO EJECUTADO** | Sin venv ni dependencias locales |
| Tests nuevos `tests/test_llm_provider_hints.py` | **NO EJECUTADOS** | Escritos, no ejecutados |
| Sonda F15.1 por término/candidato | **NO EJECUTADA** | Requiere entorno local y proxy |
| Sondas F14 | **NO EJECUTADAS** | Requieren entorno local |
| Corpus real 50/50 | **NO EJECUTADO** | Requiere proxy `:11500` y modelo real |
| `pip-audit` | **NO EJECUTADO** | Requiere instalar dependencias |
| Pruebas de PDF | **NO EJECUTADAS** | Requiere entorno local |
| Laboratorio/DB desechable | **NO EJECUTADO** | No disponible en esta sesión |

El número de 174 tests verdes y el resultado 50/50 son la base declarada por el prompt, no resultados re-ejecutados sobre este HEAD. No se certifica PASS.

## 7. Siguiente paso de validación local

En el clon local, con proxy y Docker preparados según la skill del proyecto:

1. Confirmar checkout limpio en `fix/integracion-autor-compatible`.
2. Ejecutar `python -m py_compile app/*.py`.
3. Ejecutar `.venv-test/Scripts/python.exe -m pytest tests/ -q` y confirmar que la suite no cae por debajo de la base de 174.
4. Ejecutar `pip-audit` y validar que `pypdf==6.21.4` cierra los CVE citados.
5. Ejecutar pruebas reales de extracción PDF.
6. Ejecutar la sonda `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` (0 huecos), sondas F14 y corpus `medir_corpus.py` (50/50, 0 FP, 0 FN) con el proxy comprobado.
7. Medir A/B de rendimiento con el modelo real antes de configurar hints. No habilitarlos todavía.
8. Solo cuando todo esté verde, integrar el PR en `agent-qa`.

## 8. Resumen ejecutivo

- Se curaron los diez commits del delta de upstream main y se inventariaron 50 ramas.
- Se adaptó la optimización LLM como opt-in, apagada por defecto y con reintento sin parámetros no compatibles.
- Se integró la actualización de seguridad `pypdf==6.21.4`.
- Se descartó modo ensayo por dependencia del CRM Vocero Cloud y multi-organización.
- Se descartaron CI y Dockerfile de Coolify por ser específicos del entorno del autor.
- No se tocaron F14, F15, F15.1, G1-G12 ni la cadena de vetos.
- Se añadieron cuatro tests unitarios para el fallback de hints.
- La rama tiene cambios remotos y diff revisado, pero las pruebas no se han ejecutado.
- No se actualizó `agent-qa` ni `desarrollo`.
- **Estado final: PARCIAL; requiere validación local antes de merge.**
