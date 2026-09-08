"""
Convierte crons con LISTA de horas (ej. '10,18') restando el offset a cada hora.
Complemento de convertir_cron_por_respaldo.py, que apartaba estos casos.

SEGURIDAD:
  - Solo maneja LISTAS de horas enteras separadas por coma: '10,18', '3,15'.
  - Si CUALQUIER hora de la lista hace underflow (h-offset<0) o el campo tiene
    rango '-' o step '/', se APARTA (no toca) para revision manual, porque
    cruzar medianoche puede cambiar el dia/dia-de-semana.
  - Offset por fecha del respaldo (igual criterio que el script principal).
  - --dry-run, idempotente (marca [cron-local] en Description).

Uso:
  python convertir_cron_listas.py --fecha-cambio 2026-09-06 --offset-antes 4 \
      --offset-despues 3 --respaldos respaldos --dry-run --region us-east-1
"""
import argparse
import glob
import json
import os
import re
import datetime

GRUPO = 'default'
TZ = 'America/Santiago'
MARCA = '[cron-local]'


def parse_cron(expr):
    m = re.match(r'^cron\((.+)\)$', (expr or '').strip())
    if not m:
        return None
    c = m.group(1).split()
    return c if len(c) == 6 else None


def convertir_lista_horas(expr, offset):
    """Convierte SOLO si el campo hora es una lista de enteros (a,b,c).
       Devuelve (nuevo, None) o (None, motivo)."""
    campos = parse_cron(expr)
    if not campos:
        return None, 'no-parseable'
    hora = campos[1]
    # Debe ser lista de enteros separados por coma; sin rangos ni steps
    if ',' not in hora:
        return None, 'no-es-lista'
    if '-' in hora or '/' in hora or '*' in hora:
        return None, f'lista con rango/step ({hora})'
    partes = hora.split(',')
    if not all(re.match(r'^\d{1,2}$', p) for p in partes):
        return None, f'lista no numerica ({hora})'
    nuevas = []
    for p in partes:
        n = int(p) - offset
        if n < 0:
            return None, f'underflow en {p} (cambia de dia)'
        nuevas.append(str(n))
    campos[1] = ','.join(nuevas)
    return f"cron({' '.join(campos)})", None


def nombre_sched(nombre_trigger):
    try:
        import reglas_exclusion
        return reglas_exclusion.nombre_schedule_desde_trigger(nombre_trigger)
    except Exception:
        return (nombre_trigger[:-8] + '-schedule') if nombre_trigger.endswith('-trigger') else nombre_trigger + '-schedule'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fecha-cambio', required=True)
    ap.add_argument('--offset-antes', type=int, required=True)
    ap.add_argument('--offset-despues', type=int, required=True)
    ap.add_argument('--respaldos', default='respaldos')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    corte = datetime.date.fromisoformat(args.fecha_cambio)
    mapa = {}
    for p in glob.glob(os.path.join(args.respaldos, '*.json')):
        if os.path.basename(p).startswith('_'):
            continue
        try:
            d = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        n, cron, resp = d.get('Name'), d.get('Schedule'), d.get('_respaldado_en', '')
        if not (n and cron and resp):
            continue
        offset = args.offset_antes if datetime.date.fromisoformat(resp[:10]) < corte else args.offset_despues
        mapa[nombre_sched(n)] = offset

    import boto3
    from botocore.exceptions import ClientError
    sch = boto3.client('scheduler', region_name=args.region)

    conv = apart = salt = err = 0
    apartados = []
    for name, offset in sorted(mapa.items()):
        try:
            s = sch.get_schedule(Name=name, GroupName=GRUPO)
        except ClientError:
            salt += 1; continue
        if s.get('ScheduleExpressionTimezone') != TZ or MARCA in (s.get('Description') or ''):
            salt += 1; continue
        viejo = s['ScheduleExpression']
        nuevo, motivo = convertir_lista_horas(viejo, offset)
        if nuevo is None:
            # solo reportamos los que SÍ eran lista pero con problema; el resto se ignora
            if motivo not in ('no-es-lista', 'no-parseable'):
                apart += 1; apartados.append((name, viejo, offset, motivo))
            else:
                salt += 1
            continue
        if args.dry_run:
            print(f"  [DRY] (off {offset}) {name}: {viejo} -> {nuevo}")
            conv += 1; continue
        try:
            sch.update_schedule(Name=name, GroupName=GRUPO, ScheduleExpression=nuevo,
                                ScheduleExpressionTimezone=TZ, FlexibleTimeWindow=s['FlexibleTimeWindow'],
                                Target=s['Target'], State=s['State'],
                                Description=((s.get('Description') or '') + ' ' + MARCA).strip())
            print(f"  ✅ (off {offset}) {name}: {viejo} -> {nuevo}")
            conv += 1
        except ClientError as e:
            print(f"  ❌ {name}: {e.response['Error']['Code']}"); err += 1

    print(f"\nResumen listas: {'convertiria' if args.dry_run else 'convertidos'} {conv}, "
          f"apartados {apart}, saltados {salt}, errores {err}")
    if apartados:
        print("\n⚠️  Listas con underflow (revisar a mano):")
        for n, v, o, m in apartados:
            print(f"    (off {o}) {n}: {v}  [{m}]")


if __name__ == '__main__':
    main()
