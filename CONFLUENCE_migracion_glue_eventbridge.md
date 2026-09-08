# Migración de Glue Triggers a EventBridge Scheduler

> **Estado:** ✅ Completado
> **Equipo:** BigData
> **Cuenta AWS:** 837538682169 · **Región:** us-east-1
> **Autores:** [tu nombre], con revisión de Bastian Gaete

---

## 1. Resumen ejecutivo

Migramos **~505 schedules** de **AWS Glue Triggers** a **Amazon EventBridge Scheduler**,
borramos **34 triggers obsoletos** y dejamos **77 fuera** por decisión de negocio.
El objetivo principal era **liberar la cuota de Glue Triggers** (estaba llena, impedía
crear nuevos) moviendo la programación por horario a EventBridge Scheduler, que es el
servicio recomendado por AWS para esto.

| Métrica | Valor |
|---------|-------|
| Triggers migrados a EventBridge | ~505 |
| Triggers borrados (obsoletos) | 34 |
| Triggers dejados como están (por diseño) | 77 |
| Timezone final de los schedules | America/Santiago |
| Errores en producción | 0 |

---

## 2. El problema

**AWS Glue tiene un límite (cuota) de triggers por cuenta.** Ese límite estaba
**lleno**, lo que impedía crear nuevos triggers para nuevos procesos.

### Cuotas de AWS Glue relevantes

| Cuota (Service Quotas) | Valor | Ajustable |
|------------------------|-------|-----------|
| **Max triggers per account** | **1.000** | No (nivel de cuenta) |
| Max jobs per trigger | 50 | No (nivel de cuenta) |

El límite de **1.000 triggers por cuenta** es fijo (no ajustable) y se había
alcanzado. La única forma de crear nuevos disparos por horario era **liberar
espacio** sacando de Glue los triggers que no necesitaban estar ahí.

La mayoría de esos triggers eran del tipo **SCHEDULED** (se disparan por un
cron/horario) y no necesitaban vivir en Glue: podían moverse a **EventBridge
Scheduler**, que:

- Es el servicio de AWS pensado para programación por horario.
- **No consume la cuota de triggers de Glue.**
- Soporta timezones con ajuste automático de horario de verano/invierno.

**Restricción clave:** no se podía detener ni duplicar la ejecución de los procesos.
Cada trigger debía migrarse sin que su job dejara de correr ni corriera dos veces.

---

## 3. La solución (enfoque)

Un **flujo de migración seguro, por lotes e idempotente**, controlado por un archivo
CSV que es la "fuente de verdad" del avance.

### Flujo por cada trigger (3 fases separadas)

| Fase | Qué hace | Riesgo |
|------|----------|--------|
| **1. Crear** | Crea el schedule en EventBridge **DESACTIVADO** (no dispara nada aún) | Cero: el trigger viejo sigue funcionando |
| **2. Verificar** | Compara que el schedule nuevo tenga el mismo cron y job que el trigger | Cero: solo lectura |
| **3. Switch** | Apaga el trigger viejo **y** activa el schedule nuevo | Micro-ventana controlada |

**¿Por qué fases separadas?** Permite crear decenas de schedules desactivados hoy
(sin riesgo), revisarlos con calma, y hacer el "switch" cuando se quiera. Si el
proceso se cae, se retoma leyendo el CSV (idempotente: no repite lo ya hecho).

**¿Por qué el switch apaga primero y prende después?** Preferimos una micro-ventana
sin ejecución (recuperable) antes que una **doble ejecución** (que corrompería datos).

### Arquitectura de la solución

| Componente | Rol |
|------------|-----|
| `generar_control.py` | Lee los triggers de un job y arma el CSV de control (fuente de verdad) |
| `migrar_lote.py` | Ejecuta las fases crear/verificar/switch por lotes, sobre el CSV |
| `migrar_seguro.py` | Lógica base del mapeo trigger→schedule (reusada por el lote) |
| `reglas_exclusion.py` | Reglas de exclusión y de nombres (acortado, caracteres inválidos) |
| `respaldar_triggers.py` | Guarda la definición completa de cada trigger antes de tocarlo |
| `retimezone_lote.py` | Cambia masivamente el timezone de los schedules |
| `borrar_triggers.py` | Elimina triggers obsoletos, respaldando antes |
| `reporte_seguimiento.py` | Reporte HTML: ¿qué migró y si ya disparó por EventBridge? |

---

## 4. Cómo verificamos que funcionaba

El reto: **cientos de schedules comparten el mismo job de Glue**, así que no bastaba
mirar "¿el job corrió?". Un job corriendo podía ser por cualquiera de los cientos.

Para atribuir cada corrida al schedule correcto, usamos dos señales de los "job runs":

| Señal | Qué indica |
|-------|-----------|
| `--sql_file_key` (argumento) | **Cuál** de los schedules disparó (es único por schedule) |
| `TriggerName` (metadato del run) | **Quién** lo disparó: si viene **vacío** = fue EventBridge; si trae nombre = fue el trigger viejo de Glue |

Un schedule se considera **migrado y funcionando** cuando aparece una corrida con
**su** `sql_file_key`, **posterior** al switch, y con `TriggerName` **vacío**
(disparada por EventBridge, no por el trigger viejo).

---

## 5. Decisiones clave y por qué

| Decisión | Por qué |
|----------|---------|
| **Migrar por JobName real, no por nombre del trigger** | El nombre del trigger no siempre coincide con lo que hace; filtrar por `Actions[0].JobName` atrapa exactamente los que apuntan al job objetivo. |
| **Crear el schedule DESACTIVADO primero** | Evita doble ejecución: el nuevo no dispara hasta el switch; el viejo sigue solo hasta ese momento. |
| **Switch = apagar viejo, luego prender nuevo** | Ante la duda, preferimos una micro-ventana sin ejecución (recuperable) a una doble ejecución (corrompe datos). |
| **CSV como fuente de verdad + idempotencia** | Permite pausar/retomar, escalar a cientos, y que el jefe vea el avance en Excel. |
| **Respaldo OBLIGATORIO antes de tocar** | Otro equipo elimina los triggers viejos; el respaldo es la única red para recrear si algo falla. |
| **Timezone America/Santiago (no UTC)** | El negocio piensa los horarios en hora de Chile; Santiago ajusta el horario de verano/invierno automáticamente y evita desfases. |
| **Nombres de schedule: acortar si >64 chars** | EventBridge limita el nombre a 64 caracteres. Regla del equipo: quitar prefijo `sdlf-bigdata-` y sufijo `-glue`; si aún excede, quitar `-new`. |
| **Nombres: limpiar caracteres inválidos** | EventBridge solo acepta `A-Z a-z 0-9 . - _`. Se reemplaza `ñ`→`n`, `:`→`-`, etc. |
| **No migrar triggers CONDITIONAL** | Se disparan por dependencia de otro job (workflow), no por horario. EventBridge Scheduler solo hace cron. |
| **Excluir BI y matinal** | Decisión de política del equipo (los maneja otro equipo/proceso). |

---

## 6. Casos especiales encontrados (y cómo se resolvieron)

| Caso | Cantidad | Situación | Resolución |
|------|----------|-----------|-----------|
| **Nombres >64 chars** | 32 en SEGMENTATION + 27 en redshift-to-lake | EventBridge rechaza el nombre (límite de 64 caracteres) | Regla de acortado (quitar `sdlf-bigdata-`, `-glue`, `-new`) |
| **Caracteres inválidos** (`ñ`, `:`) | 2 | EventBridge rechaza el nombre (solo acepta `A-Z a-z 0-9 . - _`) | Limpieza automática de caracteres |
| **Cron inválido** (ej. `2.3`) | 1 | Cron corrupto, EventBridge lo rechaza; se verificó que **no disparaba hace 30 días** | Apartado; negocio decidió borrarlo (duplicado muerto) |
| **Choque de sql_file_key** | 8 grupos | Varios triggers ejecutan el mismo SQL → doble ejecución preexistente | Reportado a negocio; se migraron los válidos tal cual, negocio revisa duplicados |
| **Trigger CONDITIONAL** | 31 | Workflow, no migrable a Scheduler | Se deja como Glue trigger |
| **Trigger multi-job** | 1 | Un trigger dispara 2 jobs (uno de BI) | Se deja como Glue trigger |
| **Desfase de horario** | 334+ | Schedules en UTC se desfasaron con el cambio de hora de Chile | Se pasaron todos a `America/Santiago` (ajuste DST automático) |

---

## 7. Resultado por bloque

| Bloque | Triggers | Acción |
|--------|----------|--------|
| Job `redshift-segmentation-schedule` (SEGMENTATION) | 334 | Migrados a EventBridge |
| Otros ~40 jobs | 171 | Migrados a EventBridge |
| Obsoletos (aprobados por negocio) | 34 | Borrados (con respaldo) |
| CONDITIONAL (workflow) | 31 | Dejados como Glue trigger |
| Matinal (política) | 20 | Dejados como Glue trigger |
| BI (política) | 14 | Dejados como Glue trigger |
| Inicio de workflow / ON_DEMAND / wildcard / Customer360 | 12 | Dejados como Glue trigger |

---

## 8. Cómo migrar un job nuevo (guía para quien se una)

```bash
# 0. Respaldar (red de seguridad, SIEMPRE antes de tocar)
python respaldar_triggers.py --job <NOMBRE_DEL_JOB> --region us-east-1

# 1. Generar el control del job (CSV separado por job)
python generar_control.py --job <NOMBRE_DEL_JOB> --salida control_<job>.csv --region us-east-1

# 2. Dry-run: ver qué haría, sin tocar AWS
python migrar_lote.py --paso crear-lote --control control_<job>.csv --dry-run --region us-east-1

# 3. Crear los schedules DESACTIVADOS
python migrar_lote.py --paso crear-lote --control control_<job>.csv --region us-east-1

# 4. Verificar
python migrar_lote.py --paso verificar-lote --control control_<job>.csv --region us-east-1

# 5. Switch (apaga triggers viejos + activa schedules; pide escribir MIGRAR LOTE)
python migrar_lote.py --paso switch-lote --control control_<job>.csv --region us-east-1
```

**Reglas de oro:**
1. Respaldar SIEMPRE antes de tocar.
2. Dry-run antes de crear.
3. Nunca borrar un trigger viejo hasta confirmar que su schedule disparó por EventBridge.
4. Los schedules se crean en `America/Santiago` por defecto.

---

## 9. Verificación y rollback

- **Verificar disparo real:** `reporte_seguimiento.py` genera un HTML que dice, por
  cada schedule migrado, si ya disparó por EventBridge (estado DISPARADO / PENDIENTE /
  AMBIGUO).
- **Rollback:** si un schedule falla, se recrea el trigger viejo desde su respaldo
  (`respaldos/<nombre>.json`) y se desactiva el schedule. Se vuelve al estado original.

---

## 10. Glosario

| Término | Definición |
|---------|-----------|
| **Glue Trigger** | Disparador de AWS Glue que lanza un job (por horario, evento o dependencia) |
| **EventBridge Scheduler** | Servicio de AWS para programar tareas por horario (cron), sin consumir cuota de Glue |
| **Switch** | Momento en que se apaga el trigger viejo y se activa el schedule nuevo |
| **SCHEDULED / CONDITIONAL / ON_DEMAND** | Tipos de trigger de Glue: por horario / por dependencia / manual |
| **Idempotente** | Se puede correr varias veces sin duplicar el efecto (retoma donde quedó) |
| **DST** | Horario de verano/invierno (Daylight Saving Time) |
