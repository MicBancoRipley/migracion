# Migración Glue Triggers → EventBridge Scheduler

Herramientas para migrar disparadores por horario de **AWS Glue Triggers** a
**Amazon EventBridge Scheduler** de forma **segura, por lotes e idempotente**.

Pensado para liberar la cuota de Glue Triggers moviendo la programación por
horario (cron) a EventBridge, sin detener ni duplicar la ejecución de los jobs.

> Documentación completa del proyecto (problema, decisiones, resultados):
> ver `CONFLUENCE_migracion_glue_eventbridge.md`.

---

## Requisitos

- Python 3.9+
- `boto3` (y credenciales AWS con permisos sobre Glue y EventBridge Scheduler)
- Variable de entorno con el rol del scheduler:

```bash
export SCHEDULER_ROLE_ARN="arn:aws:iam::<ACCOUNT_ID>:role/<SCHEDULER_ROLE_NAME>"
```

Para practicar sin AWS real, varios scripts aceptan `--demo` (usa `moto`).

---

## Flujo de migración (por job)

```bash
# 0. Respaldar (SIEMPRE antes de tocar)
python respaldar_triggers.py --job <JOB> --region us-east-1

# 1. Generar el archivo de control (CSV, fuente de verdad)
python generar_control.py --job <JOB> --salida control_<job>.csv --region us-east-1

# 2. Dry-run: ver qué haría sin tocar AWS
python migrar_lote.py --paso crear-lote --control control_<job>.csv --dry-run --region us-east-1

# 3. Crear schedules DESACTIVADOS
python migrar_lote.py --paso crear-lote --control control_<job>.csv --region us-east-1

# 4. Verificar (cron y job coinciden)
python migrar_lote.py --paso verificar-lote --control control_<job>.csv --region us-east-1

# 5. Switch: apaga triggers viejos + activa schedules
python migrar_lote.py --paso switch-lote --control control_<job>.csv --region us-east-1
```

**Reglas de oro**
1. Respaldar siempre antes de tocar.
2. Dry-run antes de crear.
3. No borrar un trigger viejo hasta confirmar que su schedule disparó por EventBridge.
4. Los schedules se crean en `America/Santiago` (config en `migrar_seguro.TIMEZONE`).

---

## Scripts

| Script | Rol |
|--------|-----|
| `generar_control.py` | Lee triggers de un job y arma el CSV de control (fuente de verdad) |
| `migrar_lote.py` | Ejecuta las fases crear / verificar / switch por lotes, sobre el CSV |
| `migrar_seguro.py` | Lógica base del mapeo trigger→schedule (crear/verificar/switch de a 1) |
| `reglas_exclusion.py` | Reglas de exclusión y de nombres (acortado >64, caracteres inválidos) |
| `frecuencia.py` | Clasifica los crons por frecuencia (alta / diaria / infrecuente) |
| `respaldar_triggers.py` | Guarda la definición completa de cada trigger antes de tocarlo |
| `retimezone_lote.py` | Cambia masivamente el timezone de los schedules migrados |
| `control_desde_decision.py` | Arma un control a partir de un CSV de decisiones (columna `Decision`) |
| `borrar_triggers.py` | Elimina triggers obsoletos (respaldando antes) |
| `reporte_seguimiento.py` | Reporte HTML: qué migró y si ya disparó por EventBridge |

---

## Reglas de nombres de schedule

EventBridge Scheduler limita el nombre a 64 caracteres y solo acepta
`A-Z a-z 0-9 . - _`. `reglas_exclusion.nombre_schedule_desde_trigger` aplica:

1. `-trigger` → `-schedule`.
2. Si el nombre supera 64: quitar prefijo `sdlf-bigdata-` y sufijo `-glue`;
   si aún excede, quitar sufijo `-new`.
3. Limpiar caracteres inválidos (`ñ`→`n`, `:`→`-`, etc.).

---

## Verificación y rollback

### Generar el reporte de seguimiento

Genera un HTML (`reporte_seguimiento.html`) que indica, por cada schedule
migrado, si ya disparó por EventBridge (**DISPARADO / PENDIENTE / AMBIGUO**).
Distingue el disparo real usando el `--sql_file_key` y el `TriggerName` de cada
job run (así no confunde el disparo de un schedule con el de otro que comparte job).

Dos formas equivalentes de generarlo:

```bash
# Opción 1: vía migrar_seguro (solo regenera el reporte, no migra nada)
python migrar_seguro.py --paso reporte --region us-east-1

# Opción 2: ejecutar el generador directamente
python reporte_seguimiento.py --region us-east-1
```

> Nota: `migrar_seguro.py` también regenera el reporte automáticamente después
> de los pasos que cambian estado (crear / switch), salvo que se use `--sin-reporte`.

### Rollback

Si un schedule falla, reactivar el trigger viejo desde su respaldo
(`respaldos/<nombre>.json`) y desactivar el schedule. Se vuelve al estado original.

---

## Notas

- Los archivos `control_*.csv`, `triggers_faltantes*.csv` y las carpetas
  `respaldos*/` NO se versionan (ver `.gitignore`): contienen nombres y
  configuración de procesos reales. Son artefactos de ejecución.
