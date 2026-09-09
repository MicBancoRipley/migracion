# Migración Glue Triggers → EventBridge Scheduler

Herramientas para migrar los disparadores por horario de **AWS Glue Triggers** a
**Amazon EventBridge Scheduler** de forma **segura, por lotes e idempotente**,
sin detener ni duplicar la ejecución de los jobs.

Todo se opera desde un **menú numérico** (`main.py`). Cada opción invoca el
script correcto en `scripts/` con los parámetros y el flujo seguro que se validó
en producción.

> Documentación de contexto (problema, decisiones, resultados):
> ver `CONFLUENCE_migracion_glue_eventbridge.md`.

---

## Inicio rápido

```bash
# 1. Clonar la rama (opción B: carpeta nueva y limpia)
git clone -b reestructuracion-menu https://github.com/MicBancoRipley/migracion.git migracion-final
cd migracion-final

# 2. Instalar dependencia
pip install boto3

# 3. Ejecutar el menú
python main.py
```

Al abrir el menú:
1. **Opción `0` (Configurar entorno)** — pega tus credenciales temporales.
2. **Opción `d` (Decisiones previas)** — define timezone, offset de cron y grupos
   **ANTES de migrar**. Así cada schedule nace correcto y no hay que reparar nada.
3. Luego sigue el orden numérico.

> **Idea clave (lección aprendida):** el timezone, la corrección del cron y el
> grupo se **deciden antes** y se **aplican al crear** cada schedule. La primera
> vez migramos "tal cual" y después tuvimos que reparar todo a mano (retimezone
> masivo, 486 crons, 521 cambios de grupo). Eso ya no pasa.

---

## Requisitos

- Python 3.8+
- `boto3` → `pip install boto3`
- Credenciales AWS **temporales** con permisos sobre Glue y EventBridge Scheduler
- El ARN del rol IAM que EventBridge Scheduler asume para invocar Glue

Todo esto se configura desde la **opción 0** del menú, que crea un archivo `.env`
local (nunca se sube a git; está en `.gitignore`).

---

## Configuración del `.env`

El `.env` guarda solo lo necesario. Se crea de dos formas:

**Opción A — desde el menú (recomendada):** opción `0`, pega los valores cuando
los pida.

**Opción B — manual:** copia la plantilla y edítala.

```bash
cp .env.example .env        # Linux / Mac
copy .env.example .env      # Windows CMD
Copy-Item .env.example .env # Windows PowerShell
```

Contenido:

```
# Credenciales / conexión (opción 0 del menú)
AWS_ACCESS_KEY_ID=...        # las 3 credenciales temporales del portal SSO
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
AWS_DEFAULT_REGION=us-east-1
SCHEDULER_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/<ROL_SCHEDULER>

# Decisiones previas (opción d del menú) — se aplican AL CREAR cada schedule
TIMEZONE_MIGRACION=America/Santiago
OFFSET_CRON=3                # 3=verano (UTC-3), 4=invierno (UTC-4), 0=no tocar el cron
JOB_PRINCIPAL=sdlf-bigdata-redshift-segmentation-schedule-glue-job
GRUPO_JOB_PRINCIPAL=datamanagement_stored_procedures
GRUPO_RESTO=sdlf_bigdata_glue_jobs
```

> ⚠️ Las credenciales temporales **caducan en horas**. Si ves `ExpiredToken`,
> vuelve a la opción `0` y pégalas de nuevo. (Guardar credenciales no borra las
> decisiones previas, y viceversa: el `.env` se fusiona.)

### Decisiones previas (opción `d`)

Estas tres decisiones se definen **antes de migrar** y la migración las aplica
**al crear** cada schedule:

- **Timezone** → `America/Santiago` (ajusta verano/invierno solo).
- **Offset de cron** → resta el desfase para dejar el cron en hora local.
- **Grupo** → el schedule nace directo en su grupo real (no en `default`).

Política de conversión del cron (segura):

| Caso del cron | Qué hace |
|---|---|
| hora simple / lista de horas | convierte automáticamente |
| diario de madrugada (`00:00`) | convierte (wrap inofensivo `00→21`) |
| día-específico con wrap (viernes 01:00) | **no** convierte → marca `revisar` (requiere OK de negocio) |
| alta frecuencia (cada N, rangos, `*`) | no toca (el timezone no la afecta) |

---

## El menú (`main.py`)

```
  0) Configurar entorno (.env: credenciales + ARN)   <- primero
  d) Decisiones previas (timezone + offset + grupo)  <- ANTES de migrar

  FASE 1 · PREPARACIÓN
  1) Generar inventario de triggers (control CSV)
  2) Respaldar definiciones de triggers           [red de seguridad]

  FASE 2 · MIGRACIÓN (crea YA con hora local + grupo real)
  3) Migrar UN trigger (paso a paso)
  4) Migrar por LOTES
  5) Verificar estado de conversión               [auditoría]

  REMEDIACIÓN (solo para migraciones ANTIGUAS mal creadas)
  6) Cambiar timezone  UTC -> America/Santiago
  7) Corregir crons desfasados (hora simple)
  8) Corregir crons con lista de horas (1,14,19...)
  9) Mover schedules a su grupo real

  LIMPIEZA
 10) Borrar triggers obsoletos                     [con respaldo]

  UTILIDADES
 11) Generar reporte HTML de seguimiento
 12) Guía rápida / lecciones aprendidas
```

> Las opciones **6–9 son remediación**: solo sirven para arreglar schedules
> **antiguos** que se crearon mal. Si migras con las decisiones previas
> definidas (opción `d`), **no las necesitas** — los schedules ya nacen bien.

---

## Flujo recomendado (orden completo)

1. **`0` Configurar `.env`** — credenciales + ARN del rol.
2. **`d` Decisiones previas** — timezone, offset de cron y grupos. **Antes de migrar.**
3. **`1` Inventario** — crea `control_migracion.csv`, la **fuente de verdad**.
4. **`2` Respaldar** — guarda cada definición en `respaldos/*.json` (red de seguridad).
5. **`3`/`4` Migrar** — crea los schedules **DESACTIVADOS**, ya con hora local y
   grupo real; verifica; y recién entonces hace el *switch* (apaga el viejo, prende
   el nuevo). Los crons delicados (día-específico con wrap) quedan marcados
   `revisar` y **no** se crean hasta que un humano decida.
6. **`5` Verificar** — auditoría de solo lectura para confirmar el resultado.
7. **`10` Borrar** — días después, elimina los triggers viejos (con respaldo).

**Reglas de oro:** define las decisiones antes de migrar · siempre respalda antes
de tocar · siempre `--dry-run` primero · escala de a poco (1 → 5 → 10 → masivo) ·
nunca dejes el trigger viejo y el schedule nuevo activos a la vez.

---

## Estructura del proyecto

```
migracion-final/
├── main.py                  # menú orquestador (único punto de entrada)
├── README.md
├── .env.example             # plantilla de configuración (sin secretos)
├── .gitignore
├── CONFLUENCE_...md          # documentación de contexto
└── scripts/
    ├── config_entorno.py     # crea/lee el .env: credenciales (op 0) y decisiones (op d)
    ├── reglas_migracion.py    # reglas que se aplican AL CREAR: offset de cron + grupo destino
    ├── generar_control.py     # inventario -> control_migracion.csv
    ├── respaldar_triggers.py  # respaldos/*.json
    ├── migrar_seguro.py       # migración segura de 1 trigger (crea con hora local + grupo real)
    ├── migrar_lote.py         # las mismas fases, por lotes
    ├── retimezone_lote.py     # [remediación] UTC -> America/Santiago
    ├── convertir_cron_pendientes.py    # [remediación] corrige crons de hora simple
    ├── convertir_cron_listas_horas.py  # [remediación] corrige crons con lista de horas
    ├── verificar_conversion.py         # auditoría de conversión
    ├── mover_grupo.py         # [remediación] default -> grupo real (recrear + borrar)
    ├── borrar_triggers.py     # elimina triggers obsoletos (con respaldo)
    ├── reglas_exclusion.py    # reglas de nombres y exclusiones (módulo)
    ├── reporte_seguimiento.py # reporte HTML
    └── _legacy/               # conversores antiguos (superados) - solo historial
```

Los scripts se pueden ejecutar sueltos, pero **la vía recomendada es `main.py`**:
ya aplica los parámetros y el orden correctos.

---

## Lecciones aprendidas (por qué existe cada cosa)

Estas son las decisiones y errores resueltos durante la migración real. Están
también en la **opción 12** del menú.

### La lección más importante: decidir ANTES de migrar
La primera vez migramos "tal cual" (mismo cron, sin grupo) y **después** tuvimos
que reparar todo a mano: retimezone masivo, corregir **486** crons desfasados y
mover **521** schedules de grupo. Tedioso y arriesgado. La solución de fondo:
definir timezone, offset y grupo **antes** (opción `d`) y **aplicarlos al crear**
cada schedule → nace correcto y no hay nada que reparar.

### Zona horaria
- Usar **`America/Santiago`** como timezone: ajusta verano/invierno automáticamente.
- **Trampa:** el timezone por sí solo **no** ajusta el cron. Por eso el offset se
  aplica al cron **en el mismo momento de crear** el schedule (no después).

### Offset por cambio de hora (DST)
- **Verano** Chile → offset **3** (UTC-3). **Invierno** → offset **4** (UTC-4).
- Se define en la opción `d` (`OFFSET_CRON`) según la temporada en que migras.

### Cron "wrap" (cruce de medianoche)
- Al restar el offset, una hora de madrugada puede cruzar medianoche
  (`00:00` → `21:00`).
- Para procesos **diarios** es inofensivo (corren igual cada día) → **se convierte**.
- Para **día específico** (viernes, día 12) el wrap **cambia el día** → **no se
  convierte**, se marca `revisar` para que negocio decida.

### Alta frecuencia
- Crons cada hora, cada N minutos, o con rangos amplios (`12-0`, `*/3`, `*`) **no
  se tocan**: corren igual sin importar la hora.

### Nombres de schedule
- EventBridge limita el nombre a **64 caracteres** y no acepta `:` ni `ñ`.
- Regla aplicada: quitar prefijo `sdlf-bigdata-` y sufijo `-glue`; `:` → `-`;
  `ñ` → `n`.

### Grupos
- El `GroupName` de un schedule es **inmutable**. Por eso al **crear** ya se pone
  el grupo real (no `default`), evitando el "mover" posterior.
- Si aún tienes schedules antiguos en `default`, la opción **9 (remediación)** los
  mueve de forma segura: respalda → crea DISABLED en el grupo nuevo → borra el
  viejo → activa. Así nunca hay doble ejecución.
- Regla de negocio: solo el job **principal**
  (`sdlf-bigdata-redshift-segmentation-schedule-glue-job`) va a
  `datamanagement_stored_procedures`; el resto a `sdlf_bigdata_glue_jobs`.

### Flujo seguro (crear-DISABLED / switch)
- Siempre crear el schedule **desactivado** primero, verificar, y recién entonces
  hacer el *switch* (apagar trigger viejo + activar schedule).
- Nunca dejar ambos activos: dispararían el mismo job dos veces.

### Operación
- **Siempre `--dry-run` primero** y escalar de a poco antes del masivo.
- Todos los pasos son **idempotentes**: se pueden repetir sin dañar el estado
  (marcan `[cron-local]` en la descripción para no re-convertir, y el CSV lleva el avance).

---

## Datos y seguridad

Estos archivos **no se versionan** (`.gitignore`) porque contienen nombres y
configuración de procesos reales:

- `.env` (credenciales)
- `control_*.csv`, `triggers_faltantes*.csv`
- `respaldos/`, `respaldos_borrar/`, `respaldos_grupos/`
- `reporte_seguimiento.html`, `reporte_mapeo.html`

Son artefactos de ejecución: se regeneran corriendo el menú.
