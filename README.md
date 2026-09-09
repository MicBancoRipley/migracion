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

Al abrir el menú, **empieza por la opción `0` (Configurar entorno)** para pegar
tus credenciales temporales. Luego sigue el orden numérico.

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
AWS_ACCESS_KEY_ID=...        # las 3 credenciales temporales del portal SSO
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
AWS_DEFAULT_REGION=us-east-1
SCHEDULER_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/<ROL_SCHEDULER>
```

> ⚠️ Las credenciales temporales **caducan en horas**. Si ves `ExpiredToken`,
> vuelve a la opción `0` y pégalas de nuevo.

---

## El menú (`main.py`)

```
  0) Configurar entorno (.env)                    <- empieza aquí

  FASE 1 · PREPARACIÓN
  1) Generar inventario de triggers (control CSV)
  2) Respaldar definiciones de triggers           [red de seguridad]

  FASE 2 · MIGRACIÓN
  3) Migrar UN trigger (paso a paso)
  4) Migrar por LOTES

  FASE 3 · ZONA HORARIA Y CRONS
  5) Cambiar timezone  UTC -> America/Santiago
  6) Corregir crons desfasados (hora simple)
  7) Corregir crons con lista de horas (1,14,19...)
  8) Verificar estado de conversión               [auditoría]

  FASE 4 · ORGANIZACIÓN
  9) Mover schedules a su grupo real

  FASE 5 · LIMPIEZA
 10) Borrar triggers obsoletos                     [con respaldo]

  UTILIDADES
 11) Generar reporte HTML de seguimiento
 12) Guía rápida / lecciones aprendidas
```

---

## Flujo recomendado (orden completo)

1. **`0` Configurar `.env`** — credenciales + ARN del rol.
2. **`1` Inventario** — crea `control_migracion.csv`, la **fuente de verdad**.
3. **`2` Respaldar** — guarda cada definición en `respaldos/*.json` (red de seguridad).
4. **`3`/`4` Migrar** — crea los schedules **DESACTIVADOS**, verifica, y recién
   entonces hace el *switch* (apaga el viejo, prende el nuevo).
5. **`5` Timezone** — pasa los schedules a `America/Santiago`.
6. **`6`/`7` Corregir crons** — resta el offset horario a los crons desfasados.
7. **`8` Verificar** — auditoría de solo lectura; corre antes y después.
8. **`9` Mover a grupos** — reorganiza del grupo `default` al grupo real.
9. **`10` Borrar** — días después, elimina los triggers viejos (con respaldo).

**Reglas de oro:** siempre respalda antes de tocar · siempre `--dry-run` primero ·
escala de a poco (1 → 5 → 10 → masivo) · nunca dejes el trigger viejo y el schedule
nuevo activos a la vez.

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
    ├── config_entorno.py     # crea/lee el .env (opción 0)
    ├── generar_control.py     # inventario -> control_migracion.csv
    ├── respaldar_triggers.py  # respaldos/*.json
    ├── migrar_seguro.py       # migración segura de 1 trigger (crear/verificar/switch/rollback)
    ├── migrar_lote.py         # las mismas fases, por lotes
    ├── retimezone_lote.py     # UTC -> America/Santiago
    ├── convertir_cron_pendientes.py    # corrige crons de hora simple
    ├── convertir_cron_listas_horas.py  # corrige crons con lista de horas
    ├── verificar_conversion.py         # auditoría de conversión
    ├── mover_grupo.py         # default -> grupo real (recrear + borrar)
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

### Zona horaria
- Usar **`America/Santiago`** como timezone de los schedules: ajusta verano/invierno
  automáticamente (no hay que tocar nada dos veces al año).
- **Trampa:** cambiar solo el *timezone* de un schedule **mueve su hora real de
  disparo**. Si el cron venía en hora UTC, queda desfasado → hay que restarle el
  offset al cron (opciones 6 y 7).

### Offset por cambio de hora (DST)
- Lo respaldado/migrado **antes** del cambio de hora usa **offset 4** (invierno, UTC-4).
- Lo posterior usa **offset 3** (verano, UTC-3).
- Por eso los scripts de corrección piden el offset explícito: depende de cuándo se migró.

### Cron "wrap" (cruce de medianoche)
- Al restar el offset, una hora de madrugada puede cruzar medianoche
  (`00:00` → `21:00` del día anterior).
- Para procesos **diarios** es inofensivo (corren igual cada día) → flag
  `--convertir-diario-madrugada`.
- Para procesos de **día específico** (viernes, día 12) el wrap **cambia el día**
  → requiere OK de negocio → flag `--permitir-wrap-dia`.

### Alta frecuencia
- Crons cada hora, cada N minutos, o con rangos amplios (`12-0`, `*/3`) **no
  necesitan** corrección de timezone: corren igual sin importar la hora. **No se tocan.**

### Nombres de schedule
- EventBridge limita el nombre a **64 caracteres** y no acepta `:` ni `ñ`.
- Regla aplicada: quitar prefijo `sdlf-bigdata-` y sufijo `-glue`; `:` → `-`;
  `ñ` → `n`. Los renombrados quedaron documentados durante el proceso.

### Grupos
- El `GroupName` de un schedule es **inmutable** → "mover de grupo" = **recrear + borrar**.
- El script lo hace seguro: respalda → crea DISABLED en el grupo nuevo → borra el
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
