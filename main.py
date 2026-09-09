#!/usr/bin/env python3
"""
=============================================================================
MIGRACION GLUE TRIGGERS -> EVENTBRIDGE SCHEDULER  ·  Banco Ripley
=============================================================================

Menu unico para operar toda la migracion. Cada opcion invoca el script
correspondiente en scripts/ con los parametros correctos y el flujo SEGURO
que se valido en produccion.

IDEA CLAVE (leccion aprendida):
    Las DECISIONES PREVIAS (timezone, offset de cron, grupo) se definen ANTES
    de migrar. La migracion las aplica AL CREAR, para que cada schedule NAZCA
    correcto y NO haya que reparar timezone/cron/grupos despues (eso fue lo
    tedioso la primera vez). Las opciones de correccion quedan como REMEDIACION,
    solo para migraciones antiguas que se hicieron mal.

Uso:
    python main.py

Requisitos:
    - Python 3.8+
    - boto3   (pip install boto3)
    - Credenciales AWS TEMPORALES en .env  (opcion 0 del menu)
=============================================================================
"""
import os
import sys
import subprocess

RAIZ = os.path.dirname(os.path.abspath(__file__))
DIR_SCRIPTS = os.path.join(RAIZ, 'scripts')
sys.path.insert(0, DIR_SCRIPTS)   # permite los imports planos entre modulos

import config_entorno   # noqa: E402  (vive en scripts/)
import reglas_migracion  # noqa: E402  (estado de las decisiones previas)


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


def _avisar_decisiones_si_faltan():
    """Antes de migrar, avisa si las decisiones previas no estan completas
    (offset y grupos). Devuelve True si el usuario decide continuar igual."""
    if reglas_migracion.decisiones_completas():
        return True
    print("\n  ⚠️  Las DECISIONES PREVIAS no estan completas:")
    print(reglas_migracion.resumen_decisiones())
    print("\n  Si migras asi, los schedules podrian nacer con la hora o el grupo")
    print("  equivocados y tendrias que repararlos despues (lo que queremos evitar).")
    print("  Recomendado: usa la opcion 'd' (Decisiones previas) primero.")
    return confirmar("\n  ¿Continuar de todas formas?")


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


def op_decisiones():
    titulo("d) DECISIONES PREVIAS (definir ANTES de migrar)")
    print("""  Estas 3 decisiones se aplican AL CREAR cada schedule, para que nazca
  correcto y NO haya que repararlo despues:

    1) TIMEZONE  -> hora local (America/Santiago ajusta verano/invierno solo)
    2) OFFSET    -> resta el desfase al cron (verano=3, invierno=4)
    3) GRUPO     -> el schedule nace en su grupo real (no en 'default')

  POLITICA de conversion de cron (segura):
    - hora simple / lista de horas  -> se convierte automaticamente
    - diario de madrugada (00:00)   -> se convierte (wrap inofensivo, 00->21)
    - dia-especifico con wrap        -> NO se convierte, se marca 'revisar'
                                        (el ajuste moveria el dia -> OK de negocio)
    - alta frecuencia (cada N, rangos) -> no se toca (el timezone no la afecta)
""")
    print("  Estado actual de las decisiones:")
    print(reglas_migracion.resumen_decisiones())
    print()
    if confirmar("Configurar / actualizar las decisiones ahora?"):
        config_entorno.configurar_decisiones_previas()
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
    crear     -> crea el schedule DESACTIVADO, YA con hora local + grupo real
                 (aplica las decisiones previas). Si el cron necesita criterio
                 de negocio, avisa y NO lo crea (lo deja para revisar).
    verificar -> confirma que cron/job coinciden
    switch    -> apaga el trigger viejo y activa el schedule (sin doble ejec.)
    estado    -> muestra el estado de ambos
    rollback  -> deshace el switch si algo salio mal
    limpiar   -> (dias despues) elimina el trigger viejo
  Regla clave: crea DESACTIVADO primero; nunca dejes los dos activos a la vez.
""")
    if not _avisar_decisiones_si_faltan():
        pausa(); return
    trigger = pedir("Nombre del trigger")
    if not trigger:
        pausa(); return
    paso = pedir("Paso (crear/verificar/switch/estado/rollback/limpiar)", "crear")
    correr('migrar_seguro.py', ['--trigger', trigger, '--paso', paso, '--region', region()])
    pausa()


def op_migrar_lote():
    titulo("4) MIGRAR POR LOTES")
    print("""  Escala el flujo seguro a muchos triggers, por FASES e idempotente.
  Cada schedule nace YA con hora local + grupo real (decisiones previas):
    crear-lote     -> crea schedules DESACTIVADOS (pendiente->creado).
                      Los de cron delicado (dia-especifico con wrap) se marcan
                      'revisar' y NO se crean.
    verificar-lote -> compara cada uno (creado->verificado)
    switch-lote    -> apaga trigger + activa schedule (verificado->migrado)
  SIEMPRE empieza con --dry-run y un --limit chico (5, 10) antes del masivo.
""")
    if not _avisar_decisiones_si_faltan():
        pausa(); return
    paso = pedir("Fase (crear-lote/verificar-lote/switch-lote)", "crear-lote")
    limite = pedir("Limite de filas (Enter = sin limite)", "")
    args = ['--paso', paso, '--region', region()]
    if limite:
        args += ['--limit', limite]
    if confirmar("Modo dry-run (recomendado la 1a vez)?"):
        args.append('--dry-run')
    correr('migrar_lote.py', args)
    pausa()


def op_verificar():
    titulo("5) VERIFICAR ESTADO DE CONVERSION (auditoria)")
    print("""  Recorre AWS y clasifica cada schedule:
    convertidos            (Santiago + marca [cron-local])
    PENDIENTES hora simple (revisar! -> deberian estar convertidos)
    pendientes rango/lista (apartados: alta frecuencia, ok dejarlos)
    otro timezone (UTC)    (monitores de alta frecuencia, ok dejarlos)
  No cambia nada: solo lee. Corre esto despues de migrar para confirmar.
""")
    grupo = pedir("Grupo a auditar", "default")
    correr('verificar_conversion.py', ['--region', region(), '--grupo', grupo])
    pausa()


# ---- REMEDIACION (solo para migraciones antiguas mal creadas) ----
def _aviso_remediacion():
    print("""  ⚠️  REMEDIACION: estas opciones NO deberias necesitarlas si migraste con
  las decisiones previas definidas (la migracion ya crea los schedules con la
  hora local y el grupo correctos). Sirven SOLO para arreglar schedules
  ANTIGUOS que se crearon mal (como nos paso la primera vez).
""")


def op_retimezone():
    titulo("6) [REMEDIACION] CAMBIAR TIMEZONE  UTC -> America/Santiago")
    _aviso_remediacion()
    print("""  Cambia el timezone de schedules YA creados en UTC. Recuerda: cambiar solo
  el timezone MUEVE la hora real de disparo; luego hay que corregir el cron
  (opciones 7 y 8). Idempotente.
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
    titulo("7) [REMEDIACION] CORREGIR CRONS DESFASADOS (hora simple)")
    _aviso_remediacion()
    print("""  Resta el offset horario a crons de HORA SIMPLE de schedules ya creados.
  Trabaja sobre una lista de nombres. OFFSET: verano=3, invierno=4.
  Flags:
    --convertir-diario-madrugada : diarios 00:00->21:00 (wrap inofensivo)
    --permitir-wrap-dia          : tambien los de dia especifico (OK negocio)
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
    titulo("8) [REMEDIACION] CORREGIR CRONS CON LISTA DE HORAS (1,14,19...)")
    _aviso_remediacion()
    print("""  Para schedules ya creados cuyo campo hora es una LISTA (ej. 1,14,19).
  Resta el offset a CADA hora (con wrap). Aparta rangos/steps (alta frecuencia).
""")
    lista = pedir("Archivo .txt con nombres de schedule", "listas.txt")
    offset = pedir("Offset a restar (3=verano, 4=invierno)", "3")
    args = ['--lista', lista, '--offset', offset, '--region', region()]
    if confirmar("Dry-run primero?"):
        args.append('--dry-run')
    correr('convertir_cron_listas_horas.py', args)
    pausa()


def op_mover_grupo():
    titulo("9) [REMEDIACION] MOVER SCHEDULES A SU GRUPO REAL")
    _aviso_remediacion()
    print("""  Reorganiza schedules ANTIGUOS que quedaron en 'default' a su grupo real:
    job principal -> datamanagement_stored_procedures ; resto -> sdlf_bigdata_glue_jobs
  ⚠️  El grupo es INMUTABLE: mover = recrear + borrar. El script respalda, crea
  DISABLED, borra el viejo y activa (sin doble ejecucion).
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
    d  DECISIONES PREVIAS (timezone/offset/grupo)  <- ANTES de migrar
    1  Generar inventario (control CSV)
    2  Respaldar triggers                       <- red de seguridad
    3/4 Migrar (uno o por lotes). Cada schedule NACE con hora local + grupo
        real; los de cron delicado quedan marcados 'revisar'.
    5  Verificar conversion (auditoria)
    10 Borrar obsoletos (dias despues, con respaldo)

  Las opciones 6-9 son REMEDIACION: solo para arreglar schedules ANTIGUOS
  mal creados. Con las decisiones previas definidas, NO se necesitan.

  ------------------------------------------------------------------
  POR QUE DECIDIR ANTES (la leccion mas importante):
    La primera vez migramos "tal cual" y DESPUES tuvimos que reparar todo:
    retimezone masivo, corregir 486 crons y mover 521 de grupo. Tedioso y
    arriesgado. Ahora las 3 decisiones se aplican AL CREAR -> nada que reparar.

  LECCIONES CLAVE:
  * TIMEZONE: usar America/Santiago (ajusta verano/invierno solo).
    Cambiar el timezone NO ajusta el cron -> por eso el offset lo aplicamos
    al cron en el MISMO momento de crear.
  * OFFSET por cambio de hora (DST): verano -> 3 (UTC-3); invierno -> 4 (UTC-4).
  * CRON WRAP: al restar el offset, una hora de madrugada puede cruzar
    medianoche (00:00 -> 21:00). Diario = inofensivo (se convierte).
    Dia-especifico (viernes, dia 12) = cambia el dia -> se marca 'revisar'.
  * ALTA FRECUENCIA: crons cada hora / cada N min / rangos NO se tocan.
  * NOMBRES: EventBridge no acepta ':' ni 'n~' y limita a 64 chars.
  * GRUPOS: el GroupName es INMUTABLE -> al crear ya se pone el real.
  * FLUJO SEGURO: crear DESACTIVADO -> verificar -> switch. Nunca los dos activos.
  * SIEMPRE --dry-run primero y escalar (1 -> 5 -> 10 -> masivo).
""")
    pausa()


# ---------------------------------------------------------------------------
# Menu principal
# ---------------------------------------------------------------------------
OPCIONES = {
    '0': op_configurar,
    'd': op_decisiones,
    '1': op_inventario,
    '2': op_respaldar,
    '3': op_migrar_uno,
    '4': op_migrar_lote,
    '5': op_verificar,
    '6': op_retimezone,
    '7': op_cron_simple,
    '8': op_cron_listas,
    '9': op_mover_grupo,
    '10': op_borrar,
    '11': op_reporte,
    '12': op_guia,
}


def menu():
    creds = "OK" if config_entorno.hay_credenciales() else "NO configuradas"
    decis = "OK" if reglas_migracion.decisiones_completas() else "sin definir"
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║   MIGRACION GLUE TRIGGERS -> EVENTBRIDGE SCHEDULER            ║
║   Banco Ripley · Menu de operaciones                          ║
╚══════════════════════════════════════════════════════════════╝
   Region: {region():<12}  Credenciales: {creds:<15}  Decisiones: {decis}

  0) Configurar entorno (.env: credenciales + ARN)   <- primero
  d) Decisiones previas (timezone + offset + grupo)  <- ANTES de migrar

  -- FASE 1: PREPARACION --
  1) Generar inventario de triggers (control CSV)
  2) Respaldar definiciones de triggers   [red de seguridad]

  -- FASE 2: MIGRACION (crea YA con hora local + grupo real) --
  3) Migrar UN trigger (paso a paso)
  4) Migrar por LOTES
  5) Verificar estado de conversion   [auditoria]

  -- REMEDIACION (solo para migraciones ANTIGUAS mal creadas) --
  6) Cambiar timezone  UTC -> America/Santiago
  7) Corregir crons desfasados (hora simple)
  8) Corregir crons con lista de horas (1,14,19...)
  9) Mover schedules a su grupo real

  -- LIMPIEZA --
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
        if op in ('q', 'salir'):
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
