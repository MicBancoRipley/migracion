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

CLAVES = [
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_DEFAULT_REGION',
    'SCHEDULER_ROLE_ARN',
]


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
    for k in CLAVES:
        v = datos.get(k) or os.environ.get(k, '')
        secreto = k in ('AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_ACCESS_KEY_ID')
        print(f"    {k:24} = {_enmascarar(v) if secreto else (v or '(vacio)')}")


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
    """Escribe el diccionario de valores al .env."""
    lineas = [
        "# Credenciales AWS TEMPORALES (SSO) + config del proyecto.",
        "# ESTE ARCHIVO NO DEBE SUBIRSE A GIT (esta en .gitignore).",
        "# Si las credenciales caducan (ExpiredToken), regeneralas y vuelve a guardar.",
        "",
    ]
    for k in CLAVES:
        lineas.append(f"{k}={valores.get(k, '')}")
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lineas) + '\n')
    # Permisos restrictivos donde el SO lo soporte (no en Windows, pero no falla)
    try:
        os.chmod(ruta, 0o600)
    except Exception:
        pass


def hay_credenciales():
    """True si las 3 credenciales AWS estan presentes en el entorno."""
    return all(os.environ.get(k) for k in
               ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'))


if __name__ == '__main__':
    configurar_interactivo()
