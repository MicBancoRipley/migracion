"""
=============================================================================
CONVERTIR CRON UTC -> HORA LOCAL usando reporte_mapeo.html como referencia
=============================================================================

POR QUE ESTE SCRIPT (vs convertir_cron_por_respaldo.py):
  El conversor por respaldo necesita el JSON de cada trigger en la carpeta
  respaldos/ para decidir el offset por su fecha. Pero hay ~171 schedules
  migrados (el bloque "otros jobs") que NO tienen respaldo JSON, asi que ese
  script los SALTA y quedan con el cron en UTC (desfasados).

  Este script usa reporte_mapeo.html (foto pre-migracion con los 584 triggers,
  incluye los 171 sin respaldo) como fuente del cron de referencia, y aplica un
  OFFSET FIJO que tu decides (los otros-jobs se migraron esta semana -> verano,
  offset 3). Asi cerramos el hueco sin depender de respaldos.

SEGURIDAD (igual que los otros conversores):
  - Solo convierte crons de HORA SIMPLE (aparta rangos/listas/underflow).
  - Solo toca schedules cuyo timezone ES America/Santiago.
  - Idempotente: marca [cron-local] en Description y salta los ya marcados.
  - --dry-run para revisar antes de aplicar.
  - --solo-sin-marca (default True): NO re-toca los que ya convertimos.

FLUJO RECOMENDADO:
  1. Ya corriste convertir_cron_por_respaldo.py y _listas.py (los 413 con respaldo).
  2. Corre verificar_conversion.py para ver cuantos quedan pendientes.
  3. Corre ESTE con --dry-run para ver que haria sobre los pendientes.
  4. Aplica sin --dry-run.

Uso:
  python convertir_cron_desde_mapeo.py --offset 3 --mapeo reporte_mapeo.html \
      --dry-run --region us-east-1
=============================================================================
"""
import argparse
import json
import re

GRUPO = 'default'
TZ_ESPERADA = 'America/Santiago'
MARCA = '[cron-local]'


def cargar_mapeo(ruta):
    """Extrae {schedule_name: cron_original} desde el DATOS=[...] del HTML."""
    html = open(ruta, encoding='utf-8').read()
    m = re.search(r'const DATOS = (\[.*?\]);', html, re.DOTALL)
    if not m:
        raise SystemExit("No encontre 'const DATOS = [...]' en el HTML de mapeo.")
    datos = json.loads(m.group(1))
    out = {}
    for d in datos:
        sched = d.get('schedule')
        cron = d.get('cron')
        if sched and cron:
            out[sched] = cron
    return out


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--offset', type=int, required=True,
                    help='Horas a restar (verano Chile=3, invierno=4)')
    ap.add_argument('--mapeo', default='reporte_mapeo.html')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    mapa = cargar_mapeo(args.mapeo)
    print(f"Crons de referencia en mapeo: {len(mapa)}  |  offset a restar: {args.offset}\n")

    import boto3
    from botocore.exceptions import ClientError
    scheduler = boto3.client('scheduler', region_name=args.region)

    conv = apart = salt_marca = salt_tz = salt_nomap = err = 0
    apartados = []
    for sched_name, cron_ref in sorted(mapa.items()):
        try:
            s = scheduler.get_schedule(Name=sched_name, GroupName=GRUPO)
        except ClientError:
            salt_nomap += 1   # no existe en AWS (no migrado o renombrado)
            continue
        if s.get('ScheduleExpressionTimezone') != TZ_ESPERADA:
            salt_tz += 1
            continue
        if MARCA in (s.get('Description') or ''):
            salt_marca += 1   # ya convertido
            continue

        # Convertir sobre el cron ACTUAL del schedule (UTC), no el del mapeo
        viejo = s['ScheduleExpression']
        nuevo, motivo = convertir_hora(viejo, args.offset)
        if nuevo is None:
            apart += 1
            apartados.append((sched_name, viejo, motivo))
            continue

        if args.dry_run:
            print(f"  [DRY] {sched_name}: {viejo} -> {nuevo}")
            conv += 1
            continue
        try:
            scheduler.update_schedule(
                Name=sched_name, GroupName=GRUPO,
                ScheduleExpression=nuevo, ScheduleExpressionTimezone=TZ_ESPERADA,
                FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
                State=s['State'],
                Description=((s.get('Description') or '') + ' ' + MARCA).strip())
            print(f"  ✅ {sched_name}: {viejo} -> {nuevo}")
            conv += 1
        except ClientError as e:
            print(f"  ❌ {sched_name}: {e.response['Error']['Code']}")
            err += 1

    print(f"\nResumen: {'convertiria' if args.dry_run else 'convertidos'} {conv}, "
          f"apartados {apart}, ya-marcados {salt_marca}, "
          f"no-Santiago {salt_tz}, no-en-AWS {salt_nomap}, errores {err}")
    if apartados:
        print("\n⚠️  APARTADOS (rango/lista/underflow — requieren criterio):")
        for n, v, m in apartados:
            print(f"    {n}: {v}  [{m}]")


if __name__ == '__main__':
    main()
