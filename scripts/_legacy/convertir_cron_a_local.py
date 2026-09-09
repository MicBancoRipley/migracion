"""
=============================================================================
CONVERTIR CRON UTC -> HORA LOCAL (para schedules ya en America/Santiago)
=============================================================================

PROBLEMA que resuelve:
  Al cambiar el timezone del schedule de UTC a America/Santiago con
  retimezone_lote.py, NO se ajustó la hora del cron. Resultado: un cron pensado
  para UTC (ej. 12:00 UTC = 8am Chile) ahora se interpreta como 12:00 Chile
  -> los procesos corren corridos por el offset (UTC-4 / UTC-3).

QUÉ HACE:
  Resta el offset a la HORA del cron para que la hora REAL de disparo se
  mantenga (ej. 12:00 -> 08:00 con offset 4). Solo actúa sobre schedules cuyo
  timezone ya es America/Santiago.

SEGURIDAD (no convierte a ciegas):
  - Solo convierte crons de HORA SIMPLE: cron(M H D M W Y) con H un número solo.
  - APARTA (no toca, reporta) los casos peligrosos:
      * underflow: si H - offset < 0 -> cae el día anterior, cambia día/semana.
      * hora con lista o rango: '12,16' o '10-0' o '*' -> conversión ambigua.
  - --dry-run OBLIGATORIO primero: muestra viejo -> nuevo para revisar.
  - Idempotencia: guarda marca en la Description para no convertir dos veces.

Uso:
  python convertir_cron_a_local.py --offset 4 --control control_X.csv --dry-run --region us-east-1
  python convertir_cron_a_local.py --offset 4 --control control_X.csv --region us-east-1
=============================================================================
"""
import argparse
import csv
import re

GRUPO = 'default'
TZ_ESPERADA = 'America/Santiago'
MARCA = '[cron-local]'   # marca en Description para no reconvertir


def parse_cron(expr):
    """Devuelve los 6 campos de un cron(...) de EventBridge, o None si no matchea."""
    m = re.match(r'^cron\((.+)\)$', expr.strip())
    if not m:
        return None
    campos = m.group(1).split()
    if len(campos) != 6:
        return None
    return campos  # [min, hour, day-of-month, month, day-of-week, year]


def convertir_hora(expr, offset):
    """Intenta convertir SOLO la hora de un cron simple. Devuelve:
       (nuevo_cron, None)      si convirtió bien
       (None, motivo)          si hay que apartarlo (peligroso/ambiguo)
    """
    campos = parse_cron(expr)
    if not campos:
        return None, 'no-parseable'
    minuto, hora = campos[0], campos[1]

    # hora debe ser un entero simple (sin , - / *)
    if not re.match(r'^\d{1,2}$', hora):
        return None, f'hora no simple ({hora})'

    h = int(hora)
    nueva = h - offset
    if nueva < 0:
        return None, f'underflow (H={h}-{offset}={nueva}, cambia de dia)'
    if nueva > 23:
        return None, f'overflow (H={nueva})'

    campos[1] = str(nueva)
    return f"cron({' '.join(campos)})", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--offset', type=int, required=True,
                    help='Horas a restar (Chile: 4 en invierno, 3 en verano)')
    ap.add_argument('--control', help='CSV de control para acotar a esos schedules (opcional)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    import boto3
    from botocore.exceptions import ClientError
    scheduler = boto3.client('scheduler', region_name=args.region)

    # Nombres objetivo: del control si se pasa, si no todos los del grupo default
    nombres = None
    if args.control:
        nombres = []
        with open(args.control, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f, delimiter=';'):
                if r.get('estado') == 'migrado' and r.get('schedule_name'):
                    nombres.append(r['schedule_name'].strip())

    if nombres is None:
        nombres = []
        token = None
        while True:
            kw = {'MaxResults': 100, 'GroupName': GRUPO}
            if token:
                kw['NextToken'] = token
            resp = scheduler.list_schedules(**kw)
            nombres += [s['Name'] for s in resp.get('Schedules', [])]
            token = resp.get('NextToken')
            if not token:
                break

    convertidos = apartados = saltados = errores = 0
    apartados_lista = []
    for n in nombres:
        try:
            s = scheduler.get_schedule(Name=n, GroupName=GRUPO)
        except ClientError as e:
            errores += 1
            continue

        if s.get('ScheduleExpressionTimezone') != TZ_ESPERADA:
            saltados += 1  # solo tocamos los que ya están en Santiago
            continue
        if MARCA in (s.get('Description') or ''):
            saltados += 1  # ya convertido antes (idempotente)
            continue

        viejo = s['ScheduleExpression']
        nuevo, motivo = convertir_hora(viejo, args.offset)
        if nuevo is None:
            apartados += 1
            apartados_lista.append((n, viejo, motivo))
            continue

        if args.dry_run:
            print(f"  [DRY] {n}: {viejo} -> {nuevo}")
            convertidos += 1
            continue

        try:
            scheduler.update_schedule(
                Name=n, GroupName=GRUPO,
                ScheduleExpression=nuevo,
                ScheduleExpressionTimezone=TZ_ESPERADA,
                FlexibleTimeWindow=s['FlexibleTimeWindow'],
                Target=s['Target'],
                State=s['State'],
                Description=((s.get('Description') or '') + ' ' + MARCA).strip(),
            )
            print(f"  ✅ {n}: {viejo} -> {nuevo}")
            convertidos += 1
        except ClientError as e:
            print(f"  ❌ {n}: {e.response['Error']['Code']}")
            errores += 1

    print(f"\nResumen: {'convertiría' if args.dry_run else 'convertidos'} {convertidos}, "
          f"apartados {apartados}, saltados {saltados}, errores {errores}")
    if apartados_lista:
        print(f"\n⚠️  APARTADOS (revisar a mano — cron con rango/lista o underflow):")
        for n, viejo, motivo in apartados_lista:
            print(f"    {n}: {viejo}  [{motivo}]")


if __name__ == '__main__':
    main()
