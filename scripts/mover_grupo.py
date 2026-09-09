"""
=============================================================================
MOVER SCHEDULES DE GRUPO en EventBridge Scheduler
=============================================================================

CONTEXTO:
  Todos los schedules migrados quedaron en el grupo 'default'. El equipo los
  quiere organizados por grupo real (definido por Bastian):

    - Job == 'sdlf-bigdata-redshift-segmentation-schedule-glue-job' (el PRINCIPAL,
      NO el de BI)            -> grupo 'datamanagement_stored_procedures'
    - Cualquier otro job                                -> grupo 'sdlf_bigdata_glue_jobs'
    - EXCEPCIONES que se QUEDAN en 'default' (no tocar):
        sdlf-bigdata-seguros-acoustic-import-schedule-prod
        sdlf-bigdata-seguros-acoustic-check-import-schedule-prod

IMPORTANTE (concepto AWS):
  En EventBridge Scheduler el GroupName es INMUTABLE. No hay "cambiar de grupo":
  hay que RECREAR el schedule en el grupo destino y BORRAR el del origen.

FLUJO SEGURO (por cada schedule a mover):
  1. Lee su config completa del grupo origen (get_schedule).
  2. Respalda esa config a JSON en respaldos_grupos/<grupo>__<nombre>.json.
  3. Crea copia IDENTICA en el grupo destino, pero DISABLED (no dispara aun).
  4. Borra el schedule del grupo origen.
  5. Activa (ENABLED) el del destino SOLO si el original estaba ENABLED.
  -> Orden crear-DISABLED / borrar-viejo / activar-nuevo evita doble ejecucion.

  Los grupos destino se crean si no existen (create_schedule_group, idempotente).

SEGURIDAD:
  - --dry-run obligatorio para revisar antes.
  - Idempotente: si el schedule ya esta en el grupo destino (no en origen), lo salta.
  - Respalda SIEMPRE antes de borrar.
  - Preserva TODO: cron, timezone, Target, FlexibleTimeWindow, Description, State.

USO:
  python mover_grupo.py --dry-run --region us-east-1
  python mover_grupo.py --limit 10 --region us-east-1     # primera tanda de prueba
  python mover_grupo.py --region us-east-1                # todos
=============================================================================
"""
import argparse
import json
import os
import time
import datetime

GRUPO_ORIGEN = 'default'
JOB_PRINCIPAL = 'sdlf-bigdata-redshift-segmentation-schedule-glue-job'
GRUPO_SEGMENTATION = 'datamanagement_stored_procedures'
GRUPO_RESTO = 'sdlf_bigdata_glue_jobs'
CARPETA_RESPALDOS = 'respaldos_grupos'
PAUSA = 0.2

# Schedules que se QUEDAN en default (no mover)
EXCEPCIONES = {
    'sdlf-bigdata-seguros-acoustic-import-schedule-prod',
    'sdlf-bigdata-seguros-acoustic-check-import-schedule-prod',
}


def job_del_schedule(sched):
    """Extrae el JobName del Target.Input del schedule."""
    try:
        inp = json.loads(sched['Target']['Input'])
        return (inp.get('JobName') or '').strip()
    except Exception:
        return ''


def grupo_destino(job):
    """Regla de Bastian: solo el job principal va a datamanagement; el resto a glue_jobs."""
    if job == JOB_PRINCIPAL:
        return GRUPO_SEGMENTATION
    return GRUPO_RESTO


def listar_schedules(scheduler, grupo):
    out, tok = [], None
    while True:
        kw = {'GroupName': grupo, 'MaxResults': 100}
        if tok:
            kw['NextToken'] = tok
        resp = scheduler.list_schedules(**kw)
        out.extend(s['Name'] for s in resp.get('Schedules', []))
        tok = resp.get('NextToken')
        if not tok:
            break
    return out


def asegurar_grupo(scheduler, grupo, dry_run):
    """Crea el grupo si no existe (idempotente)."""
    from botocore.exceptions import ClientError
    try:
        scheduler.get_schedule_group(Name=grupo)
        return  # ya existe
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
    if dry_run:
        print(f"  [DRY] crearia grupo '{grupo}'")
        return
    scheduler.create_schedule_group(Name=grupo)
    print(f"  ✅ grupo creado: {grupo}")


def respaldar_config(sched, grupo_origen):
    os.makedirs(CARPETA_RESPALDOS, exist_ok=True)
    nombre = sched['Name']
    data = {
        'Name': nombre,
        'GroupOrigen': grupo_origen,
        'ScheduleExpression': sched['ScheduleExpression'],
        'ScheduleExpressionTimezone': sched.get('ScheduleExpressionTimezone'),
        'FlexibleTimeWindow': sched['FlexibleTimeWindow'],
        'Target': sched['Target'],
        'State': sched['State'],
        'Description': sched.get('Description', ''),
        '_respaldado_en': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    ruta = os.path.join(CARPETA_RESPALDOS, f"{grupo_origen}__{nombre}.json")
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return ruta


def crear_en_destino(scheduler, sched, grupo, estado):
    """Crea copia identica en el grupo destino, con el estado dado."""
    scheduler.create_schedule(
        Name=sched['Name'],
        GroupName=grupo,
        ScheduleExpression=sched['ScheduleExpression'],
        ScheduleExpressionTimezone=sched.get('ScheduleExpressionTimezone', 'UTC'),
        FlexibleTimeWindow=sched['FlexibleTimeWindow'],
        Target=sched['Target'],
        State=estado,
        Description=sched.get('Description', ''),
    )


def activar_en_destino(scheduler, sched, grupo):
    s = scheduler.get_schedule(Name=sched['Name'], GroupName=grupo)
    scheduler.update_schedule(
        Name=s['Name'], GroupName=grupo,
        ScheduleExpression=s['ScheduleExpression'],
        ScheduleExpressionTimezone=s.get('ScheduleExpressionTimezone', 'UTC'),
        FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
        State='ENABLED', Description=s.get('Description', ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None, help='Mover solo N (prueba en lotes)')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    import boto3
    from botocore.exceptions import ClientError
    scheduler = boto3.client('scheduler', region_name=args.region)

    nombres = listar_schedules(scheduler, GRUPO_ORIGEN)
    print(f"Schedules en '{GRUPO_ORIGEN}': {len(nombres)}")
    print(f"Regla: job=={JOB_PRINCIPAL} -> {GRUPO_SEGMENTATION}; resto -> {GRUPO_RESTO}")
    print(f"Excepciones que se quedan en default: {len(EXCEPCIONES)}\n")

    # Asegurar grupos destino
    asegurar_grupo(scheduler, GRUPO_SEGMENTATION, args.dry_run)
    asegurar_grupo(scheduler, GRUPO_RESTO, args.dry_run)
    print()

    movidos = saltados = err = 0
    a_seg = a_resto = 0
    for nombre in nombres:
        if args.limit and movidos >= args.limit:
            print(f"\n(limit {args.limit} alcanzado)")
            break
        if nombre in EXCEPCIONES:
            saltados += 1
            continue
        try:
            sched = scheduler.get_schedule(Name=nombre, GroupName=GRUPO_ORIGEN)
        except ClientError:
            saltados += 1
            continue

        job = job_del_schedule(sched)
        destino = grupo_destino(job)
        estado_orig = sched['State']

        if args.dry_run:
            print(f"  [DRY] {nombre}")
            print(f"        job={job or '(?)'}")
            print(f"        {GRUPO_ORIGEN} -> {destino}   (estado {estado_orig})")
            movidos += 1
            if destino == GRUPO_SEGMENTATION: a_seg += 1
            else: a_resto += 1
            continue

        try:
            # 1. respaldar
            respaldar_config(sched, GRUPO_ORIGEN)
            # 2. crear en destino DISABLED
            crear_en_destino(scheduler, sched, destino, estado='DISABLED')
            # 3. borrar del origen
            scheduler.delete_schedule(Name=nombre, GroupName=GRUPO_ORIGEN)
            # 4. activar en destino si el original estaba ENABLED
            if estado_orig == 'ENABLED':
                activar_en_destino(scheduler, sched, destino)
            print(f"  ✅ {nombre}: {GRUPO_ORIGEN} -> {destino} ({estado_orig})")
            movidos += 1
            if destino == GRUPO_SEGMENTATION: a_seg += 1
            else: a_resto += 1
            time.sleep(PAUSA)
        except ClientError as e:
            code = e.response['Error']['Code']
            print(f"  ❌ {nombre}: {code}")
            err += 1

    print(f"\nResumen: {'moveria' if args.dry_run else 'movidos'} {movidos} "
          f"(-> {GRUPO_SEGMENTATION}: {a_seg}, -> {GRUPO_RESTO}: {a_resto}), "
          f"saltados {saltados} (excepciones/no-existe), errores {err}")
    if not args.dry_run and movidos:
        print(f"  Respaldos de config en: {CARPETA_RESPALDOS}/")


if __name__ == '__main__':
    main()
