# Choques de `--sql_file_key` — para revisión de negocio

**Contexto:** durante la migración Glue → EventBridge se detectó que **varios Glue
triggers distintos apuntan al MISMO archivo SQL** (`--sql_file_key`). Esto es un
**error de configuración PREEXISTENTE** (no lo causó la migración): son triggers
duplicados o con el `sql_file_key` mal copiado.

**Por qué importa:** al migrar UNO de estos triggers a EventBridge, los OTROS que
apuntan al mismo SQL siguen activos como Glue trigger → **el proceso corre por dos
(o tres) lados = doble/triple ejecución**.

Detectado de forma independiente por: (1) nuestro análisis de los respaldos y
(2) el script de auditoría del jefe (triggers vs EventBridge). Ambos coinciden.

---

## 🔴 CRÍTICOS — doble/triple ejecución AHORA (2+ triggers ACTIVATED)

### 1. `call_sp_ppff_actualizar_sav_motor_mes_actual.sql` — 3 triggers, ~367 runs/30d
| Estado | Trigger | Cron |
|--------|---------|------|
| ⚠️ ACTIVATED | `sdlf-bigdata-sp-ppff-actualizar-sav-motor-mes-actual-glue-trigger` | `cron(30 12-21 * * ? *)` (cada hora 12–21) |
| ⚠️ ACTIVATED | `sdlf-bigdata-call-sp-pff-actualizar-sav-motor-mes-actual-trigger` | `cron(0 12 ? * * *)` |
| migrado (DEACTIVATED) | `sdlf-bigdata-call-sp-pff-actualizar-av-motor-mes-actual-trigger` + schedule ENABLED | `cron(0 12 ? * * *)` |
**Acción:** negocio decide cuál es el bueno. Apagar los otros 2. El de `cron(30 12-21)` parece el activo real de alta frecuencia.

### 2. `call_sp_ctbl_lnegro_car.sql` — 2 triggers (sql_file_key mal copiado)
| Estado | Trigger | Cron |
|--------|---------|------|
| migrado (DEACTIVATED) | `sdlf-bigdata-sp-ctbl-lnegro-car-trigger` + schedule ENABLED | `cron(55 8 * * ? *)` |
| ⚠️ ACTIVATED | `sdlf-bigdata-sp-tnda-forma-pago-en-tda-glue-trigger` | `cron(00 15 ? * FRI *)` |
**Acción:** el nombre `forma-pago-en-tda` NO corresponde a `lnegro_car`. Casi seguro es un `--sql_file_key` mal copiado. Corregir el SQL de ese trigger al correcto, o apagarlo si es duplicado.

### 3. `call_sp_ppff_operaciones_dap.sql` — 2 triggers ACTIVATED
| Estado | Trigger | Cron |
|--------|---------|------|
| ⚠️ ACTIVATED | `sdlf-bigdata-sp-ppff-operaciones-dap-diario-glue-trigger` | `cron(00 19 * * ? *)` |
| ⚠️ ACTIVATED | `sdlf-bigdata-sp-ppff-operaciones-dap-glue-trigger` | `cron(45 12 * * ? *)` |
**Acción:** dos horarios distintos para el mismo SQL. ¿Intencional (2 corridas/día) o duplicado? Negocio decide.

### 4. `call_sp_run_tablon_alta_planes.sql` — 2 triggers ACTIVATED
| Estado | Trigger | Cron |
|--------|---------|------|
| ⚠️ ACTIVATED | `sdlf-bigdata-tablon-alta-planes-glue-trigger` | `cron(00 15 * * ? *)` |
| ⚠️ ACTIVATED | `sdlf-bigdata-planes-step1-trigger` | (sin cron / workflow) |
**Acción:** `planes-step1` parece parte de un workflow (step). Revisar si ambos deben correr.

---

## 🟡 REVISAR — 1 activo, pero SQL compartido (no urgente)

- `call_sp_ctbl_email_autom_refactor.sql` → `cbtl-...` [ACTIVATED] + `sp-ctbl-...` [CREATED]. (typo cbtl/ctbl, mismo proceso)
- `call_sp_mdpg_contratos_itf.sql` → `contratos-itf-tg-job` [ACTIVATED] + `sp-mdpg-contratos-itf` [CREATED]
- `call_sp_mdpg_cup_presencial.sql` → `sp-mdpg-cup-presencial` [ACTIVATED] + `sp-camp-push-cupon-capta` [CREATED]
- `call_sp_mdpg_uso_tr_marca.sql` → ambos DEACTIVATED (sin riesgo, pero SQL compartido)

---

## Recomendación operativa

1. **NO migrar** ninguno de estos triggers en las tandas automáticas hasta que negocio resuelva cada caso.
2. Apartarlos a estado `revisar` en el control de migración.
3. Los CRÍTICOS #1–#4: revisar HOY porque hay doble/triple ejecución corriendo en producción (independiente de la migración).
