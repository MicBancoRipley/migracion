# Casos en REVISAR — no migrados automáticamente

**Contexto:** de los 344 triggers del job `sdlf-bigdata-redshift-segmentation-schedule-glue-job`,
**334 se migraron** a EventBridge Scheduler. Los que quedan en `revisar` NO se
pudieron migrar de forma automática y limpia — requieren una decisión de negocio
o un tratamiento especial. Se detallan abajo por categoría.

Ninguno es un fallo de la migración: son características propias del trigger
(tipo, nombre o configuración) que EventBridge Scheduler no puede replicar tal cual.

---

## A) Tipo CONDITIONAL (workflow) — NO migrables a Scheduler

EventBridge Scheduler solo dispara por horario (cron). Estos triggers se disparan
cuando **otro job termina** (encadenamiento de workflow), no por reloj. Deben
quedarse como Glue trigger, o rehacerse con Step Functions / EventBridge Rules.

| Trigger | Tipo |
|---------|------|
| `sdlf-bigdata-planes-step1-trigger` | CONDITIONAL |
| `sdlf-bigdata-platun-step3-trigger` | CONDITIONAL |

**Acción:** dejar como están (no migrar). Si se quiere en EventBridge, es otro proyecto (Step Functions).

---

## B) Multi-acción (varios jobs en un trigger) — requiere estrategia

Este trigger dispara **más de un job** en una sola definición. Un schedule de
EventBridge invoca un `StartJobRun` a la vez, así que no se puede replicar 1:1.

| Trigger | Nº acciones |
|---------|-------------|
| `sdlf-bigdata-sp-earq-drop-tables-workspace-glue-trigger` | 2 |

**Acción:** negocio decide — ¿crear 2 schedules?, ¿unir en un job?, ¿Step Functions?

---

## C) Cron inválido en el origen

El cron del trigger está mal escrito y EventBridge lo rechaza.

| Trigger | Cron actual | Problema |
|---------|-------------|----------|
| `sdlf-bigdata-contratos-itf-tg-job` | `cron(0 10 2.3 * ? *)` | `2.3` no es un día válido (¿era `2,3` = días 2 y 3?) |

**Acción:** negocio confirma el cron correcto → luego se migra. (También es choque de SQL, ver sección E.)

---

## D) Nombre con carácter inválido para EventBridge

EventBridge Scheduler solo acepta `A-Z a-z 0-9 . - _` en el nombre. Estos tienen
caracteres no permitidos.

| Trigger | Carácter | Sugerencia |
|---------|----------|------------|
| `sdlf-bigdata-sp-clts-bases-cumpleaños-puntos-glue-trigger` | `ñ` | `cumpleaños` → `cumpleanos` |
| `sdlf-bigdata-sp-run-clts-cactivos-universo-adquisicion-10:05-glue-trigger` | `:` | `10:05` → `10-05` |

**Acción:** negocio aprueba el nombre limpio del schedule → luego se migra.

---

## E) Choques de `--sql_file_key` (varios triggers → mismo SQL)

Errores de configuración PREEXISTENTES: varios triggers ejecutan el mismo SQL.
Ver detalle completo en `CHOQUES_SQL_FILE_KEY.md`. Los que aún tienen 2+ triggers
ACTIVATED generan doble/triple ejecución:

- `call_sp_ctbl_lnegro_car.sql` → `sp-ctbl-lnegro-car` + `sp-tnda-forma-pago-en-tda` (nombre no calza, ¿sql mal copiado?)
- `call_sp_ppff_actualizar_sav_motor_mes_actual.sql` → 3 triggers (2 migrados como grupo, revisar el 3º)
- `call_sp_ppff_operaciones_dap.sql` → 2 triggers, 2 horarios
- `call_sp_run_tablon_alta_planes.sql` → `tablon-alta-planes` + `planes-step1` (este último es CONDITIONAL)

**Acción:** negocio decide cuál trigger es el válido de cada grupo.

---

## Nota sobre BI / matinal (excluidos por política, NO están en revisar por error)

Los triggers de **BI** (`bigdata-bi-`) y **matinal** se excluyen por política del
equipo desde el inicio — no entran en la migración. No requieren acción aquí.

---

## Resumen

| Categoría | Acción de quién |
|-----------|-----------------|
| A) CONDITIONAL | dejar como Glue trigger (o proyecto aparte) |
| B) Multi-acción | negocio define estrategia |
| C) Cron inválido | negocio corrige cron → migrar |
| D) Nombre inválido | negocio aprueba nombre → migrar |
| E) Choques SQL | negocio define trigger válido de cada grupo |

Una vez negocio resuelva C, D y E, esos triggers se pueden migrar con la misma
maquinaria (crear-lote / verificar-lote / switch-lote), apuntando por `--filtro`.
