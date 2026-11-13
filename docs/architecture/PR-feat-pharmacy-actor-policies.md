# PR — Paso 9: Identidad, permisos y frontera Customer/Staff

> **Rama:** `feat/pharmacy-actor-policies` · **Base:** `61b5615`
> (`feat/clinical-handoff`) · **Fecha:** 2026-11-13 · **Modo:** seguridad; requiere
> modelo de identidad aprobado.
>
> ## 🛑 Resultado: **DISEÑO + BLOQUEO** (docs-only, sin runtime)
>
> **Decisión del propietario (esta sesión):** entregar **diseño + bloqueo**, sin código
> de runtime. El producto **no dispone de identidad verificable de empleados**, así que
> por mandato del paso — *"no simules autenticación"* — **no** se implementa Staff, **no**
> se crean roles/sesiones y **no** se cambian canales de autenticación.

## 1. Resumen ejecutivo

La auditoría (puntos 1-2) confirma que NEA **no tiene identidad de empleado verificable**:
el cliente es un número de WhatsApp (no autenticado) y el "Staff" es la posesión de una
`X-API-Key` **global compartida** (una integración, no una persona). No existen tablas,
sesiones, tokens ni roles de empleado.

Conforme a la cláusula de gobierno del Paso 9 y al punto 10, se entrega:

1. **Auditoría de identidad/roles/tenant/sesiones** con evidencia a ruta:línea.
2. **Diseño server-side** de autorización por **actor × acción × recurso × farmacia**,
   deny-by-default, con resultado tipado (`Autorizado`/`Denegado`/`RequiereAclaracion`).
3. **Tenant scope en capa de repositorio** (no en prompts ni UI) + patrón de
   `pharmacy_id` obligatorio por método.
4. **Esquema de auditoría** de mutaciones Staff (actor, farmacia, acción, resultado,
   `correlation_id`), sin PII/clínico.
5. **Controles anti-IDOR / cambio de tenant / spoofing de rol / replay.**
6. **Pruebas negativas diseñadas**; solo N1 (cliente→Staff) es verificable hoy; N2-N4
   quedan BLOCKED por falta de sujeto.
7. **Bloqueo formal** del Staff Agent (Paso 11) hasta aprobar un modelo de identidad.

**No se modifica ningún archivo de `app/`.** Entrega **documental**.

## 2. Hallazgo de seguridad principal (abierto)

- **F-1 (alta, IDOR / cross-tenant de lectura):** `/analytics/top-meds` y
  `/analytics/chronic-patients` aceptan `provider_id` del query string sin ligarlo a la
  credencial (`webhook.py:398-399,440-441`); con la API-key global, cualquier poseedor
  lee analytics de cualquier farmacia. Mitigación mínima sin identidad nueva: exigir
  `provider_id == settings.provider_id` en instancia mono-tenant. **No se corrige aquí**
  (docs-only); requiere aprobación para tocar runtime.
- **F-2 (media):** `/webhook` acepta sin firma si `META_APP_SECRET` está vacío
  (`webhook.verify_signature:37-38`): exigir firma en producción.

## 3. Alcance y archivos

| Archivo | Tipo | Contenido |
|---|---|---|
| `docs/architecture/SECURITY-ACTOR-POLICIES.md` | nuevo | Auditoría + diseño completo + tenant scope + auditoría + controles + pruebas negativas |
| `docs/architecture/ADR/ADR-0008-identidad-staff-bloqueada.md` | nuevo | Decisión: diseño sí, Staff BLOQUEADO; no simular auth; deny-by-default |
| `docs/architecture/POLICY-MATRIX.md` | editado | §5: PENDIENTE → BLOQUEADO, enlaces al diseño/ADR, F-1 |
| `docs/architecture/PR-feat-pharmacy-actor-policies.md` | nuevo | Este documento |

**Ningún cambio en `app/`, `migrations/`, `tests/`.** Sin dependencias nuevas.

## 4. Cumplimiento de los 10 puntos del prompt

| # | Punto | Cómo se cumple |
|---|---|---|
| 1 | Auditar identificación real (no confundir WhatsApp con empleado) | §2.1 de `SECURITY-ACTOR-POLICIES.md`; se marca explícito que nº WhatsApp y API-key **no** son identidad de empleado |
| 2 | Documentar auth/roles/tenant/sesiones reales | §2.2 (tabla de mecanismos; sesión = NO EXISTE) |
| 3 | Diseño server-side por actor/acción/recurso/farmacia | §3 (contexto tipado + decisión tipada deny-by-default) |
| 4 | Tenant scope en repositorio/servicio, no prompts/UI | §4 (`pharmacy_id` obligatorio por método) |
| 5 | Deny por identidad desconocida/rol ausente/farmacia/ambiguo | §3.2 regla madre |
| 6 | Reusar casos de uso; Customer/Staff políticas distintas | §3.4 (dominio compartido, política delante; puerta única intacta §3.3) |
| 7 | Auditar mutación Staff (actor/farmacia/acción/resultado/corr-id) | §5 (esquema `staff_audit`, append-only, sin PII) |
| 8 | Anti IDOR/tenant/spoof/replay | §6 (tabla de controles; replay reusa idioma idempotente existente) |
| 9 | Pruebas negativas | §7 (N1 verificable; N2-N4 BLOCKED con justificación) |
| 10 | No activar Staff ni cambiar canales sin aprobación | Cumplido: entrega docs-only, 0 runtime |

## 5. Recomendación

**No mergear como feature funcional** — es un diseño + bloqueo. Acciones del propietario:

1. **Aprobar o rechazar** el modelo de identidad de empleado (desbloquea Paso 11).
2. **Decidir** si se autoriza la corrección mínima de F-1 (binding `provider_id`) y la
   exigencia de firma en producción (F-2) como hardening independiente de la identidad.
3. Hasta entonces: **fail-closed, todo Staff denegado.**
