"""
=============================================================================
CONFIGURACION DE ENTORNO (.env) — credenciales temporales + ARN scheduler
=============================================================================

Por que un .env:
  - NUNCA se ponen credenciales ni ARNs reales en el codigo (se filtrarian al
    subir a git). El .env vive SOLO en la maquina del usuario y esta en
    .gitignore, asi que jamas llega al repositorio.
  - Las credenciales de AWS via SSO son TEMPORALES (caducan en horas). El .env
    permite renovarlas rapido sin tocar codigo.

Que guarda el .env:
    AWS_ACCESS_KEY_ID       credencial temporal (SSO)
    AWS_SECRET_ACCESS_KEY   credencial temporal (SSO)
    AWS_SESSION_TOKEN       token de sesion temporal (SSO)
    AWS_DEFAULT_REGION      region (us-east-1)
    SCHEDULER_ROLE_ARN      rol IAM que EventBridge Scheduler usa para invocar Glue

Como carga:
    main.py llama a cargar_env() al arrancar, que lee el .env y exporta cada
    variable a os.environ. boto3 toma las credenciales de ahi automaticamente,
    y migrar_seguro.py toma SCHEDULER_ROLE_ARN de ahi.

NO requiere librerias externas (no usa python-dotenv): parseo propio simple.
=============================================================================
"""
import os

ARCHIVO_ENV = '.env'

# Credenciales + ARN (lo que pega el usuario en la opcion "Configurar entorno").
CLAVES = [
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_DEFAULT_REGION',
    'SCHEDULER_ROLE_ARN',
]

# DECISIONES PREVIAS a la migracion. Se definen ANTES de migrar para que cada
# schedule NAZCA correcto (timezone + hora local + grupo real) y no haya que
# repararlo despues. Las lee scripts/reglas_migracion.py.
CLAVES_DECISIONES = [
    'TIMEZONE_MIGRACION',    # timezone objetivo (America/Santiago)
    'OFFSET_CRON',           # horas a restar al cron (3=verano, 4=invierno, 0=no tocar)
    'JOB_PRINCIPAL',         # job que va al grupo de stored procedures
    'GRUPO_JOB_PRINCIPAL',   # grupo destino del job principal
    'GRUPO_RESTO',           # grupo destino del resto de jobs
]

# Todas las claves que se guardan/leen del .env.
TODAS_LAS_CLAVES = CLAVES + CLAVES_DECISIONES

# Valores por defecto de las decisiones (los mismos que usa reglas_migracion).
DEFAULTS_DECISIONES = {
    'TIMEZONE_MIGRACION': 'America/Santiago',
    'OFFSET_CRON': '0',
    'JOB_PRINCIPAL': 'sdlf-bigdata-redshift-segmentation-schedule-glue-job',
    'GRUPO_JOB_PRINCIPAL': 'datamanagement_stored_procedures',
    'GRUPO_RESTO': 'sdlf_bigdata_glue_jobs',
}


def cargar_env(ruta=ARCHIVO_ENV):
    """Lee el .env y exporta sus variables a os.environ. Silencioso si no existe.
    Devuelve dict con lo cargado (sin imprimir secretos)."""
    cargadas = {}
    if not os.path.exists(ruta):
        return cargadas
    with open(ruta, encoding='utf-8') as f:
        for linea in f:
            s = linea.strip()
            if not s or s.startswith('#') or '=' not in s:
                continue
            clave, _, valor = s.partition('=')
            clave = clave.strip()
            valor = valor.strip().strip('"').strip("'")
            if clave and valor:
                os.environ[clave] = valor
                cargadas[clave] = valor
    return cargadas


def _enmascarar(valor):
    """Muestra solo los ultimos 4 caracteres de un secreto."""
    if not valor:
        return '(vacio)'
    if len(valor) <= 4:
        return '****'
    return '****' + valor[-4:]


def estado_actual(ruta=ARCHIVO_ENV):
    """Imprime que variables estan configuradas (enmascarando secretos)."""
    if not os.path.exists(ruta):
        print(f"  No existe {ruta}. Usa la opcion de configurar para crearlo.")
        return
    datos = cargar_env(ruta)
    print(f"  Contenido de {ruta}:")
    print("    -- credenciales / conexion --")
    for k in CLAVES:
        v = datos.get(k) or os.environ.get(k, '')
        secreto = k in ('AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_ACCESS_KEY_ID')
        print(f"    {k:24} = {_enmascarar(v) if secreto else (v or '(vacio)')}")
    print("    -- decisiones previas --")
    for k in CLAVES_DECISIONES:
        v = datos.get(k) or os.environ.get(k, '') or DEFAULTS_DECISIONES.get(k, '')
        print(f"    {k:24} = {v or '(vacio)'}")


def configurar_interactivo(ruta=ARCHIVO_ENV):
    """Pide los valores por consola y escribe el .env. Para credenciales SSO
    temporales: el usuario las copia desde el portal de AWS SSO."""
    print("\n=== CONFIGURAR .env (credenciales temporales + ARN scheduler) ===")
    print("Pega tus credenciales TEMPORALES de AWS (del portal SSO / 'aws configure sso').")
    print("Deja vacio para conservar el valor actual (si ya existe).\n")

    actual = cargar_env(ruta) if os.path.exists(ruta) else {}

    def pedir(clave, descripcion, defecto=''):
        prev = actual.get(clave, defecto)
        pista = f" [{_enmascarar(prev)}]" if prev and 'SECRET' in clave or 'TOKEN' in clave else (f" [{prev}]" if prev else '')
        val = input(f"  {descripcion}{pista}: ").strip()
        return val or prev

    valores = {}
    valores['AWS_ACCESS_KEY_ID'] = pedir('AWS_ACCESS_KEY_ID', 'AWS Access Key ID')
    valores['AWS_SECRET_ACCESS_KEY'] = pedir('AWS_SECRET_ACCESS_KEY', 'AWS Secret Access Key')
    valores['AWS_SESSION_TOKEN'] = pedir('AWS_SESSION_TOKEN', 'AWS Session Token (SSO temporal)')
    valores['AWS_DEFAULT_REGION'] = pedir('AWS_DEFAULT_REGION', 'Region', actual.get('AWS_DEFAULT_REGION', 'us-east-1'))
    valores['SCHEDULER_ROLE_ARN'] = pedir('SCHEDULER_ROLE_ARN', 'ARN del rol IAM para Scheduler',
                                          actual.get('SCHEDULER_ROLE_ARN', ''))

    escribir_env(valores, ruta)
    print(f"\n  ✅ {ruta} guardado. (esta en .gitignore, no se subira a git)")
    print("  Las credenciales SSO caducan: si ves errores 'ExpiredToken', vuelve a configurar.")


def escribir_env(valores, ruta=ARCHIVO_ENV):
    """Escribe el .env FUSIONANDO con lo que ya existia. Asi guardar solo las
    credenciales no borra las decisiones previas (y viceversa). 'valores' pisa
    lo anterior solo para las claves que trae."""
    actual = cargar_env(ruta) if os.path.exists(ruta) else {}
    combinado = dict(actual)
    combinado.update({k: v for k, v in valores.items() if v is not None})

    lineas = [
        "# Credenciales AWS TEMPORALES (SSO) + decisiones de migracion.",
        "# ESTE ARCHIVO NO DEBE SUBIRSE A GIT (esta en .gitignore).",
        "# Si las credenciales caducan (ExpiredToken), regeneralas y vuelve a guardar.",
        "",
        "# --- Credenciales / conexion ---",
    ]
    for k in CLAVES:
        lineas.append(f"{k}={combinado.get(k, '')}")
    lineas += ["", "# --- Decisiones previas (se aplican AL CREAR cada schedule) ---"]
    for k in CLAVES_DECISIONES:
        lineas.append(f"{k}={combinado.get(k, DEFAULTS_DECISIONES.get(k, ''))}")

    with open(ruta, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lineas) + '\n')
    # Permisos restrictivos donde el SO lo soporte (no en Windows, pero no falla)
    try:
        os.chmod(ruta, 0o600)
    except Exception:
        pass


def configurar_decisiones_previas(ruta=ARCHIVO_ENV):
    """Pide las DECISIONES PREVIAS (timezone, offset, grupos) y las guarda en el
    .env. Estas se aplican al CREAR cada schedule para que nazca correcto y no
    haya que repararlo despues (leccion aprendida)."""
    print("\n=== DECISIONES PREVIAS (se aplican AL CREAR cada schedule) ===")
    print("Definelas ANTES de migrar. Asi cada schedule nace con la hora local y el")
    print("grupo correctos, y NO hay que reparar timezone/cron/grupos despues.\n")

    actual = cargar_env(ruta) if os.path.exists(ruta) else {}

    def actual_o_default(clave):
        return actual.get(clave) or DEFAULTS_DECISIONES.get(clave, '')

    def pedir(clave, descripcion):
        prev = actual_o_default(clave)
        val = input(f"  {descripcion} [{prev}]: ").strip()
        return val or prev

    print("  TIMEZONE: America/Santiago ajusta verano/invierno solo (recomendado).")
    tz = pedir('TIMEZONE_MIGRACION', 'Timezone objetivo')

    print("\n  OFFSET DE CRON: horas a restar al cron para llevarlo a hora local.")
    print("    3 = verano Chile (UTC-3)   |   4 = invierno (UTC-4)   |   0 = NO tocar el cron")
    print("    (Solo se convierte hora simple y listas; alta frecuencia y")
    print("     dia-especifico-con-wrap se dejan/marcan para revisar).")
    off = pedir('OFFSET_CRON', 'Offset de cron (3/4/0)')

    print("\n  GRUPOS: el schedule nace directo en su grupo real (no en 'default').")
    print("    Regla: SOLO el job principal exacto va al grupo de stored procedures;")
    print("    el resto va al grupo general de glue jobs.")
    job_p = pedir('JOB_PRINCIPAL', 'Nombre EXACTO del job principal')
    grupo_p = pedir('GRUPO_JOB_PRINCIPAL', 'Grupo destino del job principal')
    grupo_r = pedir('GRUPO_RESTO', 'Grupo destino del resto de jobs')

    escribir_env({
        'TIMEZONE_MIGRACION': tz,
        'OFFSET_CRON': off,
        'JOB_PRINCIPAL': job_p,
        'GRUPO_JOB_PRINCIPAL': grupo_p,
        'GRUPO_RESTO': grupo_r,
    }, ruta)
    print(f"\n  ✅ Decisiones guardadas en {ruta}.")
    if (off or '0').strip() in ('', '0'):
        print("  ⚠️  OFFSET_CRON=0: los crons se crearán TAL CUAL (sin ajuste de hora).")
        print("      Si tus triggers están en hora UTC, quedarán desfasados. Usa 3 o 4.")


def hay_credenciales():
    """True si las 3 credenciales AWS estan presentes en el entorno."""
    return all(os.environ.get(k) for k in
               ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'))


if __name__ == '__main__':
    configurar_interactivo()
