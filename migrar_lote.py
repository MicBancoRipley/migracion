"""
=============================================================================
MIGRACIÓN POR LOTES (escalar de 1 a 500+ triggers de forma controlada)
=============================================================================

Usa el archivo de control (control_migracion.csv) que produce generar_control.py
como FUENTE DE VERDAD. Procesa los triggers EN LOTES, por FASES separadas, y
va actualizando el estado de cada fila. Es IDEMPOTENTE: si se cae, lo vuelves
a correr y retoma donde quedó (no repite lo ya hecho).

    Reutiliza la lógica YA PROBADA EN PROD de migrar_seguro.py:
        - construir_params()  (el mapeo trigger -> schedule)
        - nombre_schedule_desde_trigger()

FASES (el mismo flujo seguro de a-1, pero para todo un lote):

    crear-lote     -> crea los schedules DESACTIVADOS de las filas 'pendiente'
                      (estado: pendiente -> creado)
    verificar-lote -> compara cada schedule creado contra su trigger
                      (estado: creado -> verificado, o -> error si no coincide)
    switch-lote    -> apaga el trigger viejo + activa el schedule
                      (estado: verificado -> migrado)

¿Por qué fases separadas y no todo de golpe?
    Puedes crear 20 schedules DESACTIVADOS hoy (cero riesgo, no disparan nada),
    revisarlos con calma / con tu jefe, y hacer el switch de los 20 mañana.

SEGURIDAD A ESCALA:
    --dry-run   : muestra qué haría, SIN llamar a AWS. Úsalo SIEMPRE primero.
    --limit N   : procesa como máximo N filas (empieza chico: 5, 10, 20...).
    --filtro T  : solo triggers cuyo nombre contiene T.
    Pausas + reintentos entre llamadas para no chocar con las quotas de la API.
    Un error en una fila NO aborta el lote: se marca 'error' y sigue.

USO TÍPICO (progresión del plan: 1 -> 5-10 -> masivo):

    python generar_control.py --region us-east-1
    python migrar_lote.py --paso crear-lote --limit 5 --dry-run
    python migrar_lote.py --paso crear-lote --limit 5
    python migrar_lote.py --paso verificar-lote --limit 5
    python migrar_lote.py --paso switch-lote --limit 5        # (¡con tu jefe!)

    # Practicar todo el ciclo con moto (sin AWS real):
    python migrar_lote.py --demo

=============================================================================
"""

import csv
import os
import time
import json
import argparse
import datetime

# Reutilizamos la lógica ya probada en producción.
import migrar_seguro
import generar_control

ARCHIVO_CONTROL = 'control_migracion.csv'

# Pausa base entre llamadas a la API (segundos) para no saturar las quotas.
PAUSA_ENTRE_LLAMADAS = 0.3
REINTENTOS_THROTTLE = 3


def ahora():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# LECTURA / ESCRITURA DEL ARCHIVO DE CONTROL
# =============================================================================

def cargar_control(ruta=ARCHIVO_CONTROL):
    if not os.path.exists(ruta):
        raise FileNotFoundError(
            f"No existe {ruta}. Córrelo primero: python generar_control.py --region ...")
    with open(ruta, 'r', encoding='utf-8-sig', newline='') as f:
        filas = list(csv.DictReader(f, delimiter=';'))
    return filas


def guardar_control(filas, ruta=ARCHIVO_CONTROL):
    """Escritura atómica: escribe a un temporal y luego reemplaza.
    Así, si el proceso se corta a mitad de escritura, el CSV original no se corrompe."""
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        escritor = csv.DictWriter(f, fieldnames=generar_control.COLUMNAS, delimiter=';')
        escritor.writeheader()
        escritor.writerows(filas)
    os.replace(tmp, ruta)


def seleccionar(filas, estado_requerido, limit=None, filtro=None,
                solo_estado_glue=None, frecuencia=None):
    """Devuelve las filas elegibles para la fase (por estado del CSV) + filtros.

    Filtros disponibles:
      - filtro:      solo triggers cuyo nombre contiene ese texto (familia)
      - frecuencia:  'alta' | 'diaria' | 'infrecuente' (según el cron del CSV)
                     para migrar en el orden del plan del jefe.

    solo_estado_glue no se aplica aquí (requiere consultar AWS); se maneja en
    crear-lote si se pide (empezar por DEACTIVATED).
    """
    import frecuencia as freq

    elegibles = [f for f in filas if f['estado'] == estado_requerido]
    if filtro:
        elegibles = [f for f in elegibles if filtro in f['trigger_name']]
    if frecuencia:
        elegibles = [f for f in elegibles
                     if freq.clasificar_frecuencia(f.get('cron', '')) == frecuencia]
    if limit is not None:
        elegibles = elegibles[:limit]
    return elegibles


# =============================================================================
# REINTENTOS ANTE THROTTLING
# =============================================================================

def con_reintentos(fn, *args, **kwargs):
    """Ejecuta fn con reintentos si AWS responde throttling/limit exceeded."""
    from botocore.exceptions import ClientError
    intento = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            codigo = e.response['Error']['Code']
            reintentables = {'ThrottlingException', 'TooManyRequestsException',
                             'LimitExceededException', 'ServiceQuotaExceededException'}
            intento += 1
            if codigo in reintentables and intento <= REINTENTOS_THROTTLE:
                espera = PAUSA_ENTRE_LLAMADAS * (2 ** intento)
                print(f"      (throttling: {codigo}) reintento {intento}/{REINTENTOS_THROTTLE} "
                      f"en {espera:.1f}s...")
                time.sleep(espera)
                continue
            raise


# =============================================================================
# FASE: CREAR-LOTE
# =============================================================================

def fase_crear_lote(glue, scheduler, filas, args):
    """Crea schedules DESACTIVADOS para las filas 'pendiente'."""
    print("\n=== FASE: CREAR-LOTE (schedules DESACTIVADOS) ===")

    if not args.dry_run and 'CAMBIAR' in migrar_seguro.SCHEDULER_ROLE_ARN:
        print("❌ SCHEDULER_ROLE_ARN es el de ejemplo. Pon el ARN real en migrar_seguro.py.")
        return

    elegibles = seleccionar(filas, 'pendiente', args.limit, args.filtro, frecuencia=args.frecuencia)
    print(f"  Filas 'pendiente' elegibles: {len(elegibles)}"
          f"{' (DRY-RUN, no se crea nada)' if args.dry_run else ''}\n")
    if not elegibles:
        print("  Nada que crear. ¿Ya están todas creadas o filtraste de más?")
        return

    from botocore.exceptions import ClientError
    creados = errores = 0

    import reglas_exclusion

    for fila in elegibles:
        nombre = fila['trigger_name']

        # BLINDAJE: regla del equipo (BI / matinal). Aunque el CSV los traiga
        # como 'pendiente', NO se crean. Se marcan 'revisar' y se saltan.
        motivo = reglas_exclusion.motivo_exclusion(nombre, fila.get('job_name', ''))
        if motivo:
            fila['estado'] = 'revisar'
            fila['nota'] = motivo
            fila['actualizado'] = ahora()
            print(f"  ⏭️  {nombre}: {motivo} -> se salta")
            continue

        try:
            trigger = con_reintentos(glue.get_trigger, Name=nombre)['Trigger']

            # Filtro opcional: empezar solo por los DEACTIVATED (los más seguros).
            if args.solo_desactivados and trigger.get('State') != 'DEACTIVATED':
                print(f"  ⏭️  {nombre}: trigger está {trigger.get('State')} (no DEACTIVATED) -> se salta")
                continue

            params = migrar_seguro.construir_params(trigger, estado='DISABLED')
            job = json.loads(params['Target']['Input'])['JobName']

            if args.dry_run:
                print(f"  [DRY] crearía {params['Name']}")
                print(f"         cron={params['ScheduleExpression']}  job={job}")
                continue

            con_reintentos(scheduler.create_schedule, **params)
            fila['estado'] = 'creado'
            fila['nota'] = ''
            fila['actualizado'] = ahora()
            creados += 1
            print(f"  ✅ creado (DISABLED): {params['Name']}")
            time.sleep(PAUSA_ENTRE_LLAMADAS)

        except ClientError as e:
            codigo = e.response['Error']['Code']
            if codigo == 'ConflictException':
                # Ya existía -> lo tratamos como 'creado' (idempotencia).
                fila['estado'] = 'creado'
                fila['nota'] = 'ya existía'
                fila['actualizado'] = ahora()
                print(f"  ⚠️  ya existía, marcado 'creado': {fila['schedule_name']}")
            else:
                fila['estado'] = 'error'
                fila['nota'] = f'{codigo}: {e.response["Error"]["Message"][:120]}'
                fila['actualizado'] = ahora()
                errores += 1
                print(f"  ❌ error en {nombre}: {codigo}")
        except Exception as e:
            fila['estado'] = 'error'
            fila['nota'] = f'{type(e).__name__}: {str(e)[:120]}'
            fila['actualizado'] = ahora()
            errores += 1
            print(f"  ❌ error en {nombre}: {type(e).__name__}: {e}")

    if not args.dry_run:
        guardar_control(filas)
        print(f"\n  Resumen crear-lote: {creados} creados, {errores} errores.")
        print(f"  👉 Siguiente: python migrar_lote.py --paso verificar-lote --limit {args.limit or ''}")


# =============================================================================
# FASE: VERIFICAR-LOTE
# =============================================================================

def fase_verificar_lote(glue, scheduler, filas, args):
    """Compara cada schedule 'creado' contra su trigger. creado -> verificado/error."""
    print("\n=== FASE: VERIFICAR-LOTE ===")
    elegibles = seleccionar(filas, 'creado', args.limit, args.filtro, frecuencia=args.frecuencia)
    print(f"  Filas 'creado' a verificar: {len(elegibles)}\n")
    if not elegibles:
        print("  Nada que verificar.")
        return

    from botocore.exceptions import ClientError
    ok = malos = 0

    for fila in elegibles:
        nombre = fila['trigger_name']
        nombre_sched = fila['schedule_name']
        try:
            trigger = con_reintentos(glue.get_trigger, Name=nombre)['Trigger']
            sched = con_reintentos(scheduler.get_schedule, Name=nombre_sched)

            t_job = trigger['Actions'][0].get('JobName')
            s_job = json.loads(sched['Target']['Input'])['JobName']
            coincide = (trigger.get('Schedule') == sched['ScheduleExpression']) and (t_job == s_job)

            if args.dry_run:
                print(f"  [DRY] verificaría {nombre_sched}: "
                      f"cron {'=' if trigger.get('Schedule')==sched['ScheduleExpression'] else '≠'}, "
                      f"job {'=' if t_job==s_job else '≠'}")
                continue

            if coincide:
                fila['estado'] = 'verificado'
                fila['nota'] = ''
                ok += 1
                print(f"  ✅ verificado: {nombre_sched}")
            else:
                fila['estado'] = 'error'
                fila['nota'] = f'no coincide: cron/job (trigger {trigger.get("Schedule")}/{t_job})'
                malos += 1
                print(f"  ❌ NO coincide: {nombre_sched}")
            fila['actualizado'] = ahora()
            time.sleep(PAUSA_ENTRE_LLAMADAS)

        except ClientError as e:
            fila['estado'] = 'error'
            fila['nota'] = f'{e.response["Error"]["Code"]}'
            fila['actualizado'] = ahora()
            malos += 1
            print(f"  ❌ error verificando {nombre_sched}: {e.response['Error']['Code']}")

    if not args.dry_run:
        guardar_control(filas)
        print(f"\n  Resumen verificar-lote: {ok} verificados, {malos} con problemas.")
        print(f"  👉 Cuando estés listo (¡con tu jefe!): "
              f"python migrar_lote.py --paso switch-lote --limit {args.limit or ''}")


# =============================================================================
# FASE: SWITCH-LOTE
# =============================================================================

def fase_switch_lote(glue, scheduler, filas, args):
    """Apaga el trigger viejo y activa el schedule. verificado -> migrado."""
    print("\n=== FASE: SWITCH-LOTE (apagar viejo -> prender nuevo) ===")
    elegibles = seleccionar(filas, 'verificado', args.limit, args.filtro, frecuencia=args.frecuencia)
    print(f"  Filas 'verificado' a migrar: {len(elegibles)}\n")
    if not elegibles:
        print("  Nada que migrar. (¿Corriste verificar-lote antes?)")
        return

    if not args.dry_run:
        print(f"  ⚠️  Vas a hacer el SWITCH REAL de {len(elegibles)} triggers.")
        print(f"      Esto apaga los Glue triggers y activa los schedules.")
        confirm = input("  Escribe 'MIGRAR LOTE' para confirmar: ").strip()
        if confirm != 'MIGRAR LOTE':
            print("  Cancelado. No se tocó nada.")
            return

    from botocore.exceptions import ClientError
    migrados = errores = 0

    for fila in elegibles:
        nombre = fila['trigger_name']
        nombre_sched = fila['schedule_name']
        try:
            if args.dry_run:
                print(f"  [DRY] apagaría trigger {nombre} y activaría {nombre_sched}")
                continue

            # 0. Consultar el ESTADO REAL del trigger viejo. Hay 3 posibles:
            #    ACTIVATED   -> está corriendo: hay que detenerlo antes de activar
            #    DEACTIVATED -> ya está parado: NO llamar stop_trigger
            #    CREATED     -> nunca arrancó: stop_trigger FALLA (InvalidInputException)
            trigger_viejo = con_reintentos(glue.get_trigger, Name=nombre)['Trigger']
            estado_glue = trigger_viejo.get('State')

            # 1. Apagar el Glue trigger viejo SOLO si estaba activo.
            if estado_glue == 'ACTIVATED':
                con_reintentos(glue.stop_trigger, Name=nombre)

            # 2. El schedule nuevo queda en el MISMO estado que tenía el trigger:
            #    - Si el trigger estaba ACTIVATED -> ENABLED (toma el relevo)
            #    - Si estaba DEACTIVATED/CREATED  -> DISABLED (NO encender algo apagado)
            estado_schedule = 'ENABLED' if estado_glue == 'ACTIVATED' else 'DISABLED'

            sched = con_reintentos(scheduler.get_schedule, Name=nombre_sched)
            con_reintentos(
                scheduler.update_schedule,
                Name=nombre_sched,
                ScheduleExpression=sched['ScheduleExpression'],
                ScheduleExpressionTimezone=sched.get('ScheduleExpressionTimezone', migrar_seguro.TIMEZONE),
                FlexibleTimeWindow=sched['FlexibleTimeWindow'],
                Target=sched['Target'],
                State=estado_schedule,
                Description=sched.get('Description', ''),
            )
            fila['estado'] = 'migrado'
            fila['nota'] = f'trigger estaba {estado_glue}; schedule quedó {estado_schedule}'
            fila['actualizado'] = ahora()
            migrados += 1
            if estado_schedule == 'ENABLED':
                print(f"  ✅ migrado: {nombre} ({estado_glue}) -> {nombre_sched} (ENABLED)")
            else:
                print(f"  ✅ migrado: {nombre} ({estado_glue}) -> {nombre_sched} (DISABLED, "
                      f"se respeta que estaba apagado)")
            time.sleep(PAUSA_ENTRE_LLAMADAS)

        except ClientError as e:
            fila['estado'] = 'error'
            fila['nota'] = f'switch falló: {e.response["Error"]["Code"]}'
            fila['actualizado'] = ahora()
            errores += 1
            print(f"  ❌ error en switch de {nombre}: {e.response['Error']['Code']}")

    if not args.dry_run:
        guardar_control(filas)
        print(f"\n  Resumen switch-lote: {migrados} migrados, {errores} errores.")
        print(f"  👉 Monitorea las próximas ejecuciones. Actualiza el reporte:")
        print(f"     python migrar_seguro.py --paso reporte")


# =============================================================================
# RESUMEN DEL AVANCE
# =============================================================================

def mostrar_resumen(filas):
    from collections import Counter
    conteo = Counter(f['estado'] for f in filas)
    total = len(filas)
    print("\n  --- Avance de la migración ---")
    for estado in ['pendiente', 'creado', 'verificado', 'migrado', 'revisar', 'error']:
        n = conteo.get(estado, 0)
        if n:
            barra = '█' * int(30 * n / total) if total else ''
            print(f"    {estado:<12} {n:>4}  {barra}")
    print(f"    {'-'*12} {'-'*4}")
    print(f"    {'TOTAL':<12} {total:>4}")


PASOS = {
    'crear-lote': fase_crear_lote,
    'verificar-lote': fase_verificar_lote,
    'switch-lote': fase_switch_lote,
}


# =============================================================================
# MAIN
# =============================================================================

def main():
    args = parse_args()

    if args.paso == 'resumen':
        filas = cargar_control()
        mostrar_resumen(filas)
        return

    if args.demo:
        _correr_demo(args)
        return

    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError
    try:
        filas = cargar_control()
        if not args.paso:
            mostrar_resumen(filas)
            print("\n  Indica una fase con --paso crear-lote|verificar-lote|switch-lote")
            return
        glue = boto3.client('glue', region_name=args.region)
        scheduler = boto3.client('scheduler', region_name=args.region)
        PASOS[args.paso](glue, scheduler, filas, args)
        mostrar_resumen(filas)
    except FileNotFoundError as e:
        print(f"❌ {e}")
    except NoCredentialsError:
        print("❌ No hay credenciales AWS. Configúralas o usa --demo.")
    except ClientError as e:
        print(f"❌ Error de AWS: {e}")


def _correr_demo(args):
    """Ciclo completo con moto: genera control, crea, verifica y hace switch."""
    import boto3
    from moto import mock_aws

    @mock_aws
    def _demo():
        migrar_seguro.SCHEDULER_ROLE_ARN = 'arn:aws:iam::111111111111:role/DemoRole'
        glue = boto3.client('glue', region_name=args.region)
        scheduler = boto3.client('scheduler', region_name=args.region)

        # Crear triggers de prueba (3 migrables).
        for i in range(1, 4):
            glue.create_job(Name=f'sdlf-bigdata-job-{i}', Role='arn:aws:iam::111111111111:role/R',
                            Command={'Name': 'glueetl', 'ScriptLocation': 's3://x/j.py', 'PythonVersion': '3'})
            glue.create_trigger(Name=f'sdlf-bigdata-ok-{i}-glue-trigger', Type='SCHEDULED',
                                Schedule=f'cron(0 {i} * * ? *)',
                                Actions=[{'JobName': f'sdlf-bigdata-job-{i}'}])

        print("### DEMO: generando archivo de control ###")
        generar_control.generar(glue)

        filas = cargar_control()
        print("### DEMO: crear-lote (dry-run) ###")
        args.dry_run = True
        fase_crear_lote(glue, scheduler, filas, args)

        print("\n### DEMO: crear-lote (real) ###")
        args.dry_run = False
        fase_crear_lote(glue, scheduler, filas, args)

        filas = cargar_control()
        print("\n### DEMO: verificar-lote ###")
        fase_verificar_lote(glue, scheduler, filas, args)

        filas = cargar_control()
        print("\n### DEMO: switch-lote (auto-confirmado en demo) ###")
        # En demo evitamos el input() haciendo el switch a mano por fila.
        for fila in [f for f in filas if f['estado'] == 'verificado']:
            glue.stop_trigger(Name=fila['trigger_name'])
            s = scheduler.get_schedule(Name=fila['schedule_name'])
            scheduler.update_schedule(
                Name=s['Name'], ScheduleExpression=s['ScheduleExpression'],
                FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
                State='ENABLED', Description=s.get('Description', ''))
            fila['estado'] = 'migrado'
            fila['actualizado'] = ahora()
            print(f"  ✅ (demo) migrado: {fila['trigger_name']}")
        guardar_control(filas)

        print("\n### DEMO: idempotencia (correr crear-lote otra vez no debe crear nada) ###")
        filas = cargar_control()
        fase_crear_lote(glue, scheduler, filas, args)

        filas = cargar_control()
        mostrar_resumen(filas)

    _demo()


def parse_args():
    p = argparse.ArgumentParser(description='Migración por lotes con archivo de control')
    p.add_argument('--paso', choices=list(PASOS.keys()) + ['resumen'],
                   help='Fase a ejecutar')
    p.add_argument('--limit', type=int, default=None, help='Máximo de filas a procesar')
    p.add_argument('--filtro', help='Solo triggers cuyo nombre contiene este texto')
    p.add_argument('--frecuencia', choices=['alta', 'diaria', 'infrecuente'],
                   help='Solo triggers de esta frecuencia (según el cron). '
                        'Plan del jefe: migrar primero alta, luego diaria.')
    p.add_argument('--solo-desactivados', action='store_true',
                   help='En crear-lote: solo procesar triggers que ya están DEACTIVATED (lo más seguro)')
    p.add_argument('--dry-run', action='store_true', help='Mostrar qué haría, sin llamar a AWS')
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--demo', action='store_true', help='Practicar el ciclo completo con moto')
    return p.parse_args()


if __name__ == '__main__':
    main()
