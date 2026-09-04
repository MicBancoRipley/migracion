# Casos en REVISAR — los 10 no migrados

**Estado final:** de 344 triggers del job `sdlf-bigdata-redshift-segmentation-schedule-glue-job`,
**334 migrados** a EventBridge Scheduler, **10 en revisar** (detalle abajo).

Ninguno es fallo de la migración: son características del trigger (tipo, nombre,
configuración o política) que impiden migrarlo automáticamente y limpio.

---

## A) Tipo CONDITIONAL (workflow) — NO migrables a Scheduler — 2

Se disparan cuando **otro job termina** (encadenamiento), no por reloj. Scheduler
solo hace cron.

| Trigger | Nota |
|---------|------|
| `sdlf-bigdata-planes-step1-trigger` | CONDITIONAL (step de workflow, sin cron) |
| `sdlf-bigdata-platun-step3-trigger` | CONDITIONAL (step de workflow, sin cron) |

**Acción:** dejar como Glue trigger. Si se quiere en AWS-nativo: Step Functions / EventBridge Rules (otro proyecto).

## B) Multi-acción (varios jobs en un trigger) — 1

| Trigger | Cron | Nota |
|---------|------|------|
| `sdlf-bigdata-sp-earq-drop-tables-workspace-glue-trigger` | `cron(00 5 * * ? *)` | Dispara 2 jobs; un schedule invoca 1 |

**Acción:** negocio define estrategia (2 schedules / unir jobs / Step Functions).

## C) Cron inválido en el origen — 1

| Trigger | Cron | Nota |
|---------|------|------|
| `sdlf-bigdata-contratos-itf-tg-job` | `cron(0 10 2.3 * ? *)` | `2.3` no es día válido (¿`2,3`?). También choque SQL. |

**Acción:** negocio confirma el cron correcto → migrable.

## D) Nombre con carácter inválido para EventBridge — 2

EventBridge solo acepta `A-Z a-z 0-9 . - _`.

| Trigger | Carácter | Sugerencia |
|---------|----------|------------|
| `sdlf-bigdata-sp-clts-bases-cumpleaños-puntos-glue-trigger` | `ñ` | `cumpleaños`→`cumpleanos` |
| `sdlf-bigdata-sp-run-clts-cactivos-universo-adquisicion-10:05-glue-trigger` | `:` | `10:05`→`10-05` |

**Acción:** negocio aprueba nombre limpio del schedule → migrable.

## E) Choque de SQL (revisar antes de tocar) — 1

| Trigger | Cron | Nota |
|---------|------|------|
| `sdlf-bigdata-sp-tnda-forma-pago-en-tda-glue-trigger` | `cron(00 15 ? * FRI *)` | Apunta a `call_sp_ctbl_lnegro_car.sql` (nombre no calza). Posible sql_file_key mal copiado. |

**Acción:** negocio confirma si el SQL es el correcto (ver `CHOQUES_SQL_FILE_KEY.md`).

## F) Excluidos por política del equipo (matinal) — 3

Excluidos desde el inicio por decisión del equipo. No requieren acción.

| Trigger |
|---------|
| `sdlf-bigdata-sp-matinal-step-0` |
| `sdlf-bigdata-sp-matinal_parte-2-trigger` |
| `sdlf-bigdata-sp-spos-alianzas-matinal-glue-trigger` |

---

## Resumen

| Cat | Qué es | Cant | ¿Migrable tras arreglo de negocio? |
|-----|--------|------|-----------------------------------|
| A | CONDITIONAL (workflow) | 2 | No (nunca a Scheduler) |
| B | Multi-acción | 1 | Requiere diseño |
| C | Cron inválido | 1 | Sí, corrigiendo cron |
| D | Nombre inválido | 2 | Sí, aprobando nombre |
| E | Choque SQL | 1 | Sí, confirmando SQL |
| F | Matinal (política) | 3 | No (excluidos por diseño) |
| | **TOTAL** | **10** | |

Los de C, D y E se migran con la misma maquinaria (`crear-lote --filtro <nombre>`)
una vez negocio resuelva. A y F quedan fuera por diseño; B requiere decisión.
