# Resolución de pendientes — SEGMENTATION

Respuesta punto por punto a los pendientes reportados en la auditoría. Cada caso
con su **decisión** (tomada con Bastian) y su **estado final**.

Criterio general acordado: **migrar tal cual funciona hoy** (no corregir config de
negocio); los casos no migrables a Scheduler se dejan como Glue trigger.

---

## 1) Duplicados (riesgo de doble ejecución)

### 1.1 `call_sp_run_clts_cactivos_universo_adquisicion.sql`
Dos triggers al mismo SQL, distinto horario:
- `sp-run-clts-cactivos-universo-adquisicion` → `cron(25 16)` — **migrado** (schedule ENABLED)
- `sp-run-clts-cactivos-universo-adquisicion-10:05` → `cron(5 14)` — tenía `:` en el nombre

**Decisión (Bastian D):** migrar ambos tal cual (dos horarios = dos schedules).
**Estado:** ✅ RESUELTO. El de las 10:05 se migró con nombre limpio
`...universo-adquisicion-10-05-schedule` (`:` → `-`, regla de limpieza aprobada).

### 1.2 `call_sp_run_tablon_alta_planes.sql`
- `tablon-alta-planes` → `cron(00 15)` — **migrado**
- `planes-step1` → CONDITIONAL (workflow), 483 runs/30d

**Decisión (Bastian A):** los CONDITIONAL se **dejan como triggers** (Scheduler no
hace encadenamiento de workflow).
**Estado:** ✅ RESUELTO. `planes-step1` queda como Glue trigger (por diseño).

### 1.3 `call_sp_ctbl_lnegro_car.sql`
- `sp-ctbl-lnegro-car` → diario `cron(55 8)` — **migrado**
- `sp-tnda-forma-pago-en-tda` → `cron(00 15 FRI)`, apunta al mismo SQL (posible sql_file_key mal copiado)

**Verificación:** se confirmó que `forma-pago-en-tda` **SÍ dispara** (corrida real
lanzada por su trigger). No es proceso muerto.
**Decisión (Bastian E):** migrar tal cual, con nombre `sp_ctbl_lnegro_car`.
**Estado:** ✅ RESUELTO. Schedule `sp_ctbl_lnegro_car` creado (viernes 15:00),
trigger viejo apagado. Ambos SQL coexisten a sus horarios, como hoy.

---

## 2) Pendientes de migrar (4)

### 2.1 `sp-clts-bases-cumpleaños-puntos` — `cron(00 14 1 * ? *)`
Ojo con la `ñ` en el nombre (inválida para EventBridge).
**Decisión (Bastian D):** aceptar sugerencia de renombrado.
**Estado:** ✅ RESUELTO. Migrado con nombre `...cumpleanos-puntos-...` (`ñ` → `n`).

### 2.2 `sp-earq-drop-tables-workspace` — `cron(00 5)`, diario
Multi-acción: dispara 2 jobs, uno de ellos es **`sdlf-bigdata-bi-...`** (equipo BI, excluido).
**Decisión (Bastian B):** dejar como trigger.
**Estado:** ✅ RESUELTO. Queda como Glue trigger (involucra job de BI, excluido por política; además es multi-job no replicable con un schedule).

### 2.3 `sp-spos-alianzas-matinal` — `cron(00 15)`
Contiene "matinal".
**Decisión (Bastian F):** dejar como trigger.
**Estado:** ✅ RESUELTO. Queda como Glue trigger (política: procesos matinal excluidos).

### 2.4 `platun-step3` — CONDITIONAL, 531 runs/30d
**Decisión (Bastian A):** dejar como trigger.
**Estado:** ✅ RESUELTO. Queda como Glue trigger (workflow, Scheduler no aplica).

---

## Extra) `contratos-itf-tg-job` — duplicado roto
`cron(0 10 2.3 * ? *)` inválido → se verificó **0 corridas en 30 días** (no dispara).
Duplicado de `sp-mdpg-contratos-itf` (ya migrado).
**Decisión (Bastian C):** borrarlo, está duplicado.
**Estado:** ✅ RESUELTO. Trigger **eliminado** de Glue.

---

## Estado final SEGMENTATION

| Resolución | Casos |
|------------|-------|
| ✅ Migrados tal cual | 1.1 (10:05), 1.3 (E lnegro), 2.1 (cumpleaños) |
| ✅ Dejados como Glue trigger (por diseño) | 1.2 (planes-step1), 2.2 (drop-tables/BI), 2.3 (matinal), 2.4 (platun-step3) |
| ✅ Eliminado (duplicado roto) | contratos-itf-tg-job |

**No quedan pendientes abiertos en SEGMENTATION.** Los que siguen como Glue trigger
es por decisión explícita (workflow CONDITIONAL, multi-job con BI, o política matinal),
no por falta de migración.
