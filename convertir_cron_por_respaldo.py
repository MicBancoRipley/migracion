"""
=============================================================================
CONVERTIR CRON UTC -> HORA LOCAL usando la FECHA del respaldo para el offset
=============================================================================

Contexto:
  Los schedules estan en America/Santiago pero su cron quedo en hora UTC (no se
  ajusto al cambiar el timezone). Ademas, se migraron en dos momentos con offset
  distinto de Chile por el cambio de hora:
    - respaldados ANTES del cambio  -> offset invierno (ej. 4)
    - respaldados DESPUES del cambio -> offset verano  (ej. 3)

Este script decide el offset de CADA trigger segun su '_respaldado_en' (fecha
guardada en su JSON de respaldo), y convierte la hora del cron restando ese
offset. Asi cada proceso mantiene su hora local esperada, sin separar a mano.

SEGURIDAD (igual que convertir_cron_a_local.py):
  - Solo convierte crons de HORA SIMPLE. Aparta rangos/listas/underflow.
  - --dry-run obligatorio. Idempotente (marca en Description).
  - Solo toca schedules cuyo timezone ya es America/Santiago.

Uso:
  python convertir_cron_por_respaldo.py --fecha-cambio 2026-09-06 \
      --offset-antes 4 --offset-despues 3 \
      --respaldos respaldos --dry-run --region us-east-1
=============================================================================
"""
import argparse
import glob
import json
import os
import re
import datetime

GRUPO = 'default'
TZ_ESPERADA = 'America/Santiago'
MARCA = '[cron-local]'


def parse_cron(expr):
    m = re.match(r'^cron\((.+)\)$', (expr or '').strip())
    if not m:
        return None
    campos = m.group(1).split()
    return campos if len(campos) == 6 else None


def convertir_hora(expr, offset):
    campos = parse_cron(expr)
    if not campos:
        return None, 'no-parseable'
    hora = campos[1]
    if not re.match(r'^\d{1,2}$', hora):
        return None, f'hora no simple ({hora})'
    nueva = int(hora) - offset
    if nueva < 0:
        return None, f'underflow (H={hora}-{offset}={nueva})'
    if nueva > 23:
        return None, f'overflow (H={nueva})'
    campos[1] = str(nueva)
    return f"cron({' '.join(campos)})", None


def nombre_schedule_desde_trigger(nombre_trigger):
    """Mismo criterio del proyecto; para casos raros usar reglas_exclusion."""
    try:
        import reglas_exclusion
        return reglas_exclusion.nombre_schedule_desde_trigger(nombre_trigger)
    except Exception:
        if nombre_trigger.endswith('-trigger'):
            return nombre_trigger[:-len('-trigger')] + '-schedule'
        return nombre_trigger + '-schedule'


def cargar_respaldos(carpeta, fecha_cambio, off_antes, off_despues):
    """Devuelve {schedule_name: (cron_original, offset)} leyendo los JSON."""
    corte = datetime.date.fromisoformat(fecha_cambio)
    out = {}
    for p in glob.glob(os.path.join(carpeta, '*.json')):
        if os.path.basename(p).startswith('_'):
            continue
        try:
            d = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        nombre_trigger = d.get('Name')
        cron = d.get('Schedule')
        resp = d.get('_respaldado_en', '')
        if not (nombre_trigger and cron and resp):
            continue
        fecha = datetime.date.fromisoformat(resp[:10])
        offset = off_antes if fecha < corte else off_despues
        out[nombre_schedule_desde_trigger(nombre_trigger)] = (cron, offset)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fecha-cambio', required=True, help='YYYY-MM-DD del cambio de hora')
    ap.add_argument('--offset-antes', type=int, required=True)
    ap.add_argument('--offset-despues', type=int, required=True)
    ap.add_argument('--respaldos', default='respaldos')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    mapa = cargar_respaldos(args.respaldos, args.fecha_cambio,
                            args.offset_antes, args.offset_despues)
    print(f"Respaldos leidos con cron+fecha: {len(mapa)}")
    print(f"Corte: {args.fecha_cambio}  |  antes={args.offset_antes}h  despues={args.offset_despues}h\n")

    import boto3
    from botocore.exceptions import ClientError
    scheduler = boto3.client('scheduler', region_name=args.region)

    conv = apart = salt = err = 0
    apartados = []
    for sched_name, (cron_resp, offset) in sorted(mapa.items()):
        try:
            s = scheduler.get_schedule(Name=sched_name, GroupName=GRUPO)
        except ClientError:
            salt += 1
            continue
        if s.get('ScheduleExpressionTimezone') != TZ_ESPERADA:
            salt += 1
            continue
        if MARCA in (s.get('Description') or ''):
            salt += 1
            continue

        # Convertimos SOBRE el cron actual del schedule (que es el UTC original)
        viejo = s['ScheduleExpression']
        nuevo, motivo = convertir_hora(viejo, offset)
        if nuevo is None:
            apart += 1
            apartados.append((sched_name, viejo, offset, motivo))
            continue

        if args.dry_run:
            print(f"  [DRY] (off {offset}) {sched_name}: {viejo} -> {nuevo}")
            conv += 1
            continue
        try:
            scheduler.update_schedule(
                Name=sched_name, GroupName=GRUPO,
                ScheduleExpression=nuevo, ScheduleExpressionTimezone=TZ_ESPERADA,
                FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
                State=s['State'],
                Description=((s.get('Description') or '') + ' ' + MARCA).strip())
            print(f"  ✅ (off {offset}) {sched_name}: {viejo} -> {nuevo}")
            conv += 1
        except ClientError as e:
            print(f"  ❌ {sched_name}: {e.response['Error']['Code']}")
            err += 1

    print(f"\nResumen: {'convertiria' if args.dry_run else 'convertidos'} {conv}, "
          f"apartados {apart}, saltados {salt}, errores {err}")
    if apartados:
        print("\n⚠️  APARTADOS (convertir a mano — rango/lista/underflow):")
        for n, v, off, m in apartados:
            print(f"    (off {off}) {n}: {v}  [{m}]")


if __name__ == '__main__':
    main()
