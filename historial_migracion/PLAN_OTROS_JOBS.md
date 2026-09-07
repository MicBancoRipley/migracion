# Plan de trabajo — otros jobs (lista definitiva de Bastian)

Bastian revisó los 267 triggers pendientes (otros jobs, fuera de SEGMENTATION)
y decidió qué hacer con cada uno. Resumen y plan de ejecución abajo.

## Resumen por decisión

| Decisión | Cantidad | Acción |
|----------|----------|--------|
| 🟢 **migrar** | 159 | Migrar a EventBridge Scheduler (con nuestra maquinaria) |
| 🗑️ **borrar** | 31 | Eliminar el trigger de Glue (Bastian confirmó que sobran) |
| ⏸️ CONDITIONAL | 31 | Dejar como está (workflow por dependencia, no aplica Scheduler) |
| ⏸️ matinal | 20 | Dejar como está (política del equipo) |
| ⏸️ BI (`bigdata-bi-`) | 14 | Dejar como está (política del equipo) |
| ⏸️ WORKFLOW (inicio) | 6 | Dejar como está (inicia un Glue Workflow) |
| ⏸️ ON_DEMAND | 4 | Dejar como está (no tienen cron que migrar) |
| ⏸️ wildcard | 1 | Dejar (comodín, migrarlo perdería visibilidad del disparo) |
| ⏸️ Customer360 | 1 | Dejar (debe correr por otro flujo) |
| **TOTAL** | **267** | |

**Trabajo neto:** 159 migrar + 31 borrar = **190 acciones**. Los 77 restantes no se tocan.

## Notas importantes (criterios de Bastian)

- **borrar (31):** triggers que Bastian marcó como obsoletos/duplicados. Muchos
  están en `redshift-triggers-glue-job` (estado CREATED, nunca activados) y
  algunos DEACTIVATED. Eliminar con `migrar_seguro.py --paso limpiar`.
- **WORKFLOW / "Inicio de Glue Workflow":** triggers `step0` que arrancan un
  workflow por cron. Aunque son SCHEDULED, NO migrar: son la cabeza de un
  encadenamiento. Migrar solo el step0 rompería la cadena.
- **wildcard:** `indicadores-macroeconomicos` corre bajo un comodín; migrarlo
  quitaría visibilidad de qué lanza el wildcard.
- **Customer360:** debe correr por otro flujo (posible QA), no migrar.

## Jobs con más triggers a MIGRAR (orden sugerido)

1. `redshift-to-lake-glue-job` — el más grande (~45 a migrar)
2. `workloads-data-quality-psh-glue-job` — ~13
3. `data-extraction-glue-job` — ~10
4. `dataextraction-runner`, `process-txt-files`, `uncompress-z-files` — grupos medianos
5. Muchos jobs con 1-4 triggers cada uno

## Flujo por job (mismo de SEGMENTATION, ya probado)

Para cada job a migrar:
1. `respaldar_triggers.py --job <job>` (respaldo antes de tocar)
2. `generar_control.py --job <job> --salida control_<job>.csv` (CSV por job)
3. `migrar_lote.py --paso crear-lote --control control_<job>.csv --dry-run`
4. crear → verificar → switch (con `--control`)

Los schedules ya salen en `America/Santiago` (timezone corregido).

## Los 31 a BORRAR (acción separada)

No se migran: se eliminan directamente. Para cada uno:
`python migrar_seguro.py --paso limpiar --trigger <nombre> --region us-east-1`
(pide confirmar escribiendo ELIMINAR). Conviene respaldarlos ANTES por si acaso.
