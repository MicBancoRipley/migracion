#!/usr/bin/env python3
"""
=============================================================================
MIGRACION GLUE TRIGGERS -> EVENTBRIDGE SCHEDULER  ·  Banco Ripley
=============================================================================

Menu unico para operar toda la migracion. Cada opcion invoca el script
correspondiente en scripts/ con los parametros correctos y el flujo SEGURO
que se valido en produccion.

Uso:
    python main.py

Requisitos:
    - Python 3.8+
    - boto3   (pip install boto3)
    - Credenciales AWS TEMPORALES en .env  (opcion 0 del menu)

Toda la logica esta en scripts/. Los conversores antiguos (superados por los
definitivos) estan en scripts/_legacy/ solo por historial.
=============================================================================
"""
import os
import sys
import subprocess

RAIZ = os.path.dirname(os.path.abspath(__file__))
DIR_SCRIPTS = os.path.join(RAIZ, 'scripts')
sys.path.insert(0, DIR_SCRIPTS)   # permite los imports planos entre modulos

import config_entorno   # noqa: E402  (vive en scripts/)


# ---------------------------------------------------------------------------
# Helpers de consola
# ---------------------------------------------------------------------------
def limpiar():
    os.system('cls' if os.name == 'nt' else 'clear')


def pausa():
    input("\n  (Enter para volver al menu) ")


def titulo(txt):
    print("\n" + "=" * 66)
    print(f"  {txt}")
    print("=" * 66)


def confirmar(pregunta):
    return input(f"  {pregunta} (escribe 'si'): ").strip().lower() == 'si'


def pedir(txt, defecto=None):
    extra = f" [{defecto}]" if defecto else ""
    val = input(f"  {txt}{extra}: ").strip()
    return val or (defecto or "")


def correr(script_rel, args, necesita_credenciales=True):
    """Ejecuta un script de scripts/ como subproceso, heredando el entorno
    (con las variables del .env ya cargadas). Muestra el comando que corre."""
    if necesita_credenciales and not config_entorno.hay_credenciales():
        print("\n  ⚠️  No hay credenciales AWS cargadas. Usa la opcion 0 (Configurar entorno).")
        return
    ruta = os.path.join(DIR_SCRIPTS, script_rel)
    cmd = [sys.executable, ruta] + args
    print(f"\n  > python scripts/{script_rel} {' '.join(args)}\n")
    try:
        subprocess.run(cmd, cwd=RAIZ, env=os.environ.copy())
    except KeyboardInterrupt:
        print("\n  (interrumpido por el usuario)")


def region():
    return os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')


# ---------------------------------------------------------------------------
# Opciones del menu
# ---------------------------------------------------------------------------
def op_configurar():
    titulo("0) CONFIGURAR ENTORNO (.env)")
    print("""  Pega tus credenciales TEMPORALES de AWS (portal SSO -> cuenta ->
  'Command line or programmatic access'). Caducan en horas: si ves
  'ExpiredToken' mas adelante, vuelve aqui y pegalas de nuevo.
""")
    config_entorno.estado_actual()
    print()
    if confirmar("Configurar / actualizar el .env ahora?"):
        config_entorno.configurar_interactivo()
        config_entorno.cargar_env()   # recargar al entorno actual
    pausa()


def op_inventario():
    titulo("1) GENERAR INVENTARIO (control_migracion.csv)")
    print("""  Lee TODOS tus Glue triggers y crea control_migracion.csv, la FUENTE
  DE VERDAD del proceso. Cada fila = un trigger con su estado (pendiente,
  creado, verificado, migrado, revisar, error).
  Es idempotente: usa --fusionar para regenerar sin perder el avance.
""")
    args = ['--region', region()]
    if confirmar("Fusionar con un control existente (conservar estados)?"):
        args.append('--fusionar')
    correr('generar_control.py', args)
    pausa()


def op_respaldar():
    titulo("2) RESPALDAR DEFINICIONES DE TRIGGERS")
    print("""  Guarda la definicion COMPLETA de cada trigger en respaldos/<nombre>.json.
  Es la RED DE SEGURIDAD: si algo falla tras migrar, se recrea desde aqui.
  Hazlo SIEMPRE antes de migrar o borrar.
""")
    print("  a) Respaldar TODOS los del control CSV")
    print("  b) Respaldar los de un JOB especifico")
    print("  c) Respaldar UN trigger por nombre")
    op = pedir("Opcion (a/b/c)", "a").lower()
    args = ['--region', region()]
    if op == 'b':
        job = pedir("Nombre del job")
        if job:
            args += ['--job', job]
    elif op == 'c':
        tr = pedir("Nombre del trigger")
        if tr:
            args += ['--trigger', tr]
    correr('respaldar_triggers.py', args)
    pausa()


def op_migrar_uno():
    titulo("3) MIGRAR UN TRIGGER (paso a paso)")
    print("""  Flujo SEGURO validado en prod, en pasos separados:
    crear     -> crea el schedule DESACTIVADO (no compite con el viejo)
    verificar -> confirma que cron/job coinciden
    switch    -> apaga el trigger viejo y activa el schedule (sin doble ejec.)
    estado    -> muestra el estado de ambos
    rollback  -> deshace el switch si algo salio mal
    limpiar   -> (dias despues) elimina el trigger viejo
  Regla clave: crea DESACTIVADO primero; nunca dejes los dos activos a la vez.
""")
    trigger = pedir("Nombre del trigger")
    if not trigger:
        pausa(); return
    paso = pedir("Paso (crear/verificar/switch/estado/rollback/limpiar)", "crear")
    correr('migrar_seguro.py', ['--trigger', trigger, '--paso', paso, '--region', region()])
    pausa()


def op_migrar_lote():
    titulo("4) MIGRAR POR LOTES")
    print("""  Escala el flujo seguro a muchos triggers, por FASES e idempotente:
    crear-lote     -> crea schedules DESACTIVADOS (estado pendiente->creado)
    verificar-lote -> compara cada uno (creado->verificado)
    switch-lote    -> apaga trigger + activa schedule (verificado->migrado)
  SIEMPRE empieza con --dry-run y un --limit chico (5, 10) antes del masivo.
""")
    paso = pedir("Fase (crear-lote/verificar-lote/switch-lote)", "crear-lote")
    limite = pedir("Limite de filas (Enter = sin limite)", "")
    args = ['--paso', paso, '--region', region()]
    if limite:
        args += ['--limit', limite]
    if confirmar("Modo dry-run (recomendado la 1a vez)?"):
        args.append('--dry-run')
    correr('migrar_lote.py', args)
    pausa()


def op_retimezone():
    titulo("5) CAMBIAR TIMEZONE  UTC -> America/Santiago")
    print("""  Los schedules se crean pensando en hora de Chile. America/Santiago
  ajusta verano/invierno automaticamente (evita el desfase que tuvimos).
  ⚠️  LECCION APRENDIDA: cambiar SOLO el timezone MUEVE la hora real de
  disparo. Si el cron estaba en hora UTC, quedara desfasado y hay que
  corregir el cron despues (opciones 6 y 7). Idempotente.
""")
    args = ['--region', region()]
    limite = pedir("Limite (Enter = todos)", "")
    if limite:
        args += ['--limit', limite]
    if confirmar("Dry-run primero?"):
        args.append('--dry-run')
    correr('retimezone_lote.py', args)
    pausa()


def op_cron_simple():
    titulo("6) CORREGIR CRONS DESFASADOS (hora simple)")
    print("""  Resta el offset horario a crons de HORA SIMPLE (ej. cron(0 12 ...)),
  devolviendolos a su hora local real. Trabaja sobre una lista de nombres.
  OFFSET: verano Chile = 3, invierno = 4 (segun cuando se respaldo/migro).
  Flags aprendidos:
    --convertir-diario-madrugada : convierte diarios 00:00->21:00 (wrap inofensivo)
    --permitir-wrap-dia          : convierte tambien los de dia especifico
                                   (viernes, dia 12) -> aprobado por negocio
  Marca [cron-local] en Description (idempotente, no re-toca).
""")
    lista = pedir("Archivo .txt con nombres de schedule", "pendientes.txt")
    offset = pedir("Offset a restar (3=verano, 4=invierno)", "3")
    args = ['--lista', lista, '--offset', offset, '--region', region()]
    if confirmar("Convertir diarios de madrugada (00:00->21:00)?"):
        args.append('--convertir-diario-madrugada')
    if confirmar("Permitir wrap de dia (viernes/dia-N)? [requiere OK negocio]"):
        args.append('--permitir-wrap-dia')
    if confirmar("Dry-run primero?"):
        args.append('--dry-run')
    correr('convertir_cron_pendientes.py', args)
    pausa()


def op_cron_listas():
    titulo("7) CORREGIR CRONS CON LISTA DE HORAS (1,14,19...)")
    print("""  Para crons cuyo campo hora es una LISTA (ej. cron(0 1,14,19 ...)).
  Resta el offset a CADA hora de la lista (con wrap si cruza medianoche).
  Aparta rangos con guion (12-0) y steps (*/3): esos son alta frecuencia,
  el timezone no los afecta -> NO se convierten (leccion aprendida).
""")
    lista = pedir("Archivo .txt con nombres de schedule", "listas.txt")
    offset = pedir("Offset a restar (3=verano, 4=invierno)", "3")
    args = ['--lista', lista, '--offset', offset, '--region', region()]
    if confirmar("Dry-run primero?"):
        args.append('--dry-run')
    correr('convertir_cron_listas_horas.py', args)
    pausa()


def op_verificar():
    titulo("8) VERIFICAR ESTADO DE CONVERSION (auditoria)")
    print("""  Recorre AWS y clasifica cada schedule:
    convertidos            (Santiago + marca [cron-local])
    PENDIENTES hora simple (revisar! -> deberian estar convertidos)
    pendientes rango/lista (apartados: alta frecuencia, ok dejarlos)
    otro timezone (UTC)    (monitores de alta frecuencia, ok dejarlos)
  No cambia nada: solo lee. Corre esto antes y despues de convertir.
""")
    grupo = pedir("Grupo a auditar", "default")
    correr('verificar_conversion.py', ['--region', region(), '--grupo', grupo])
    pausa()


def op_mover_grupo():
    titulo("9) MOVER SCHEDULES A SU GRUPO REAL")
    print("""  Reorganiza los schedules del grupo 'default' a su grupo real:
    job == redshift-segmentation-schedule (el PRINCIPAL) -> datamanagement_stored_procedures
    cualquier otro job                                   -> sdlf_bigdata_glue_jobs
    2 seguros-acoustic-*-prod                            -> se quedan en default
  ⚠️  En EventBridge el grupo es INMUTABLE: mover = recrear + borrar. El script
  respalda, crea DISABLED, borra el viejo y activa (sin doble ejecucion).
  SIEMPRE --dry-run y luego --limit 5 antes del masivo.
""")
    args = ['--region', region()]
    limite = pedir("Limite (Enter = todos; usa 5 para probar)", "")
    if limite:
        args += ['--limit', limite]
    if confirmar("Dry-run primero?"):
        args.append('--dry-run')
    correr('mover_grupo.py', args)
    pausa()


def op_borrar():
    titulo("10) BORRAR TRIGGERS OBSOLETOS (con respaldo)")
    print("""  Elimina los Glue triggers marcados 'borrar' en el CSV de decisiones.
  SIEMPRE respalda antes (respaldos_borrar/). Los ACTIVATED requieren
  --incluir-activos explicito (borrar uno vivo detiene un proceso).
""")
    args = ['--region', region()]
    if confirmar("Incluir triggers ACTIVOS (peligroso)?"):
        args.append('--incluir-activos')
    if confirmar("Dry-run primero (recomendado)?"):
        args.append('--dry-run')
    correr('borrar_triggers.py', args)
    pausa()


def op_reporte():
    titulo("11) GENERAR REPORTE HTML DE SEGUIMIENTO")
    print("  Genera reporte_seguimiento.html con el estado de los schedules migrados.\n")
    correr('reporte_seguimiento.py', ['--region', region()])
    pausa()


def op_guia():
    titulo("12) GUIA RAPIDA / LECCIONES APRENDIDAS")
    print("""
  ORDEN RECOMENDADO DEL PROCESO COMPLETO:
    0  Configurar .env (credenciales + ARN)     <- primero SIEMPRE
    1  Generar inventario (control CSV)
    2  Respaldar triggers                       <- red de seguridad
    3/4 Migrar (uno o por lotes, DESACTIVADO primero)
    5  Cambiar timezone a America/Santiago
    6/7 Corregir crons desfasados (simple / listas)
    8  Verificar conversion (auditoria)
    9  Mover a grupos reales
    10 Borrar obsoletos (dias despues, con respaldo)

  LECCIONES CLAVE (por que existe cada cosa):
  ------------------------------------------------------------------
  * TIMEZONE: usar America/Santiago (ajusta verano/invierno solo).
    OJO: cambiar el timezone NO ajusta el cron -> si el cron estaba en
    hora UTC, queda desfasado. Hay que restar el offset al cron.
  * OFFSET por cambio de hora (DST): respaldados/migrados ANTES del
    cambio -> offset 4 (invierno UTC-4); DESPUES -> offset 3 (verano UTC-3).
  * CRON WRAP: al restar el offset, una hora de madrugada puede cruzar
    medianoche (00:00 -> 21:00 del dia anterior). Para procesos DIARIOS es
    inofensivo (corren igual cada dia). Para dia-especifico (viernes, dia 12)
    cambia el dia -> requiere OK de negocio.
  * ALTA FRECUENCIA: crons cada hora / cada N min / rangos amplios NO
    necesitan correccion de timezone (corren igual). No los toques.
  * NOMBRES: EventBridge no acepta ':' ni 'n~' y limita a 64 chars. Regla:
    quitar prefijo 'sdlf-bigdata-' y '-glue'; ':'->'-'; 'n~'->'n'.
  * GRUPOS: el GroupName es INMUTABLE -> mover = recrear + borrar.
  * FLUJO SEGURO: crear DESACTIVADO -> verificar -> switch (apagar viejo,
    prender nuevo). Nunca los dos activos (doble ejecucion corrompe datos).
  * SIEMPRE --dry-run primero y escalar (1 -> 5 -> 10 -> masivo).
""")
    pausa()


# ---------------------------------------------------------------------------
# Menu principal
# ---------------------------------------------------------------------------
OPCIONES = {
    '0': op_configurar,
    '1': op_inventario,
    '2': op_respaldar,
    '3': op_migrar_uno,
    '4': op_migrar_lote,
    '5': op_retimezone,
    '6': op_cron_simple,
    '7': op_cron_listas,
    '8': op_verificar,
    '9': op_mover_grupo,
    '10': op_borrar,
    '11': op_reporte,
    '12': op_guia,
}


def menu():
    creds = "OK" if config_entorno.hay_credenciales() else "NO configuradas"
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║   MIGRACION GLUE TRIGGERS -> EVENTBRIDGE SCHEDULER            ║
║   Banco Ripley · Menu de operaciones                          ║
╚══════════════════════════════════════════════════════════════╝
   Region: {region():<12}   Credenciales: {creds}

  0) Configurar entorno (.env: credenciales + ARN)   <- empieza aqui

  -- FASE 1: PREPARACION --
  1) Generar inventario de triggers (control CSV)
  2) Respaldar definiciones de triggers   [red de seguridad]

  -- FASE 2: MIGRACION --
  3) Migrar UN trigger (paso a paso)
  4) Migrar por LOTES

  -- FASE 3: ZONA HORARIA Y CRONS --
  5) Cambiar timezone  UTC -> America/Santiago
  6) Corregir crons desfasados (hora simple)
  7) Corregir crons con lista de horas (1,14,19...)
  8) Verificar estado de conversion   [auditoria]

  -- FASE 4: ORGANIZACION --
  9) Mover schedules a su grupo real

  -- FASE 5: LIMPIEZA --
 10) Borrar triggers obsoletos   [con respaldo]

  -- UTILIDADES --
 11) Generar reporte HTML de seguimiento
 12) Guia rapida / lecciones aprendidas

  q) Salir
""")


def main():
    config_entorno.cargar_env()   # carga .env al entorno si existe
    while True:
        menu()
        op = input("  Opcion: ").strip().lower()
        if op in ('q', 'salir', '0q'):
            print("\n  Hasta luego.\n")
            break
        accion = OPCIONES.get(op)
        if accion:
            accion()
        else:
            print("  Opcion no valida.")
            pausa()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelado.\n")
