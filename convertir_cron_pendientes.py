"""
=============================================================================
CONVERTIR CRON UTC -> HORA LOCAL sobre una LISTA EXPLICITA de schedules
=============================================================================

POR QUE ESTE (vs los otros conversores):
  - convertir_cron_por_respaldo.py  -> necesita el JSON de respaldo (fecha).
  - convertir_cron_desde_mapeo.py   -> necesita que el schedule este en el HTML.
  Ambos SALTAN los schedules que no tienen ni respaldo ni entrada de mapeo
  (p.ej. los renombrados 15:30 -> 15-30, o creados despues del reporte).

  Este NO depende de respaldos ni de mapeo. Trabaja sobre una lista EXPLICITA
  de nombres de schedule (los que verificar_conversion.py reporta como
  "PENDIENTES hora simple") y convierte SOBRE EL CRON ACTUAL en AWS, que es el
  UTC original. Le pasas el offset (verano Chile=3, invierno=4).

COMO OBTENER LA LISTA:
  1. Corre: python verificar_conversion.py --region us-east-1
  2. Copia los nombres bajo "HORA SIMPLE SIN CONVERTIR" a un archivo .txt,
     uno por linea (puedes dejar el " | cron(...)" al lado; lo ignoramos).
  3. Corre este script con --lista ese archivo.

SEGURIDAD:
  - Solo hora simple (aparta rango/lista/underflow/overflow).
  - Solo toca schedules cuyo timezone ES America/Santiago (no toca UTC).
  - Idempotente: marca [cron-local] y salta los ya marcados.
  - --dry-run para revisar.

Uso:
  python verificar_conversion.py --region us-east-1 > barrido.txt
  # edita pendientes.txt con los nombres de "HORA SIMPLE SIN CONVERTIR"
  python convertir_cron_pendientes.py --lista pendientes.txt --offset 3 \
      --dry-run --region us-east-1
=============================================================================
"""
import argparse
import re

GRUPO = 'default'
TZ_ESPERADA = 'America/Santiago'
MARCA = '[cron-local]'


def cargar_lista(ruta):
    """Lee nombres de schedule, uno por linea. Ignora todo lo que venga tras
    el primer espacio o '|' (por si pegaste '<nombre> | cron(...)'). Ignora
    lineas vacias y comentarios (#)."""
    nombres = []
    for linea in open(ruta, encoding='utf-8'):
        s = linea.strip()
        if not s or s.startswith('#'):
            continue
        # cortar en el primer '|' o espacio
        s = re.split(r'\s*\|\s*|\s+', s)[0]
        if s:
            nombres.append(s)
    # de-dup preservando orden
    vistos, out = set(), []
    for n in nombres:
        if n not in vistos:
            vistos.add(n); out.append(n)
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
    ap.add_argument('--lista', required=True, help='Archivo .txt con nombres de schedule (uno por linea)')
    ap.add_argument('--offset', type=int, required=True, help='Horas a restar (verano=3, invierno=4)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    nombres = cargar_lista(args.lista)
    print(f"Schedules en la lista: {len(nombres)}  |  offset a restar: {args.offset}\n")

    import boto3
    from botocore.exceptions import ClientError
    scheduler = boto3.client('scheduler', region_name=args.region)

    conv = apart = salt_marca = salt_tz = salt_nomap = err = 0
    apartados = []
    for nombre in nombres:
        try:
            s = scheduler.get_schedule(Name=nombre, GroupName=GRUPO)
        except ClientError:
            print(f"  ⏭️  no existe en AWS: {nombre}")
            salt_nomap += 1
            continue
        if s.get('ScheduleExpressionTimezone') != TZ_ESPERADA:
            salt_tz += 1
            continue
        if MARCA in (s.get('Description') or ''):
            salt_marca += 1
            continue

        viejo = s['ScheduleExpression']
        nuevo, motivo = convertir_hora(viejo, args.offset)
        if nuevo is None:
            apart += 1
            apartados.append((nombre, viejo, motivo))
            continue

        if args.dry_run:
            print(f"  [DRY] {nombre}: {viejo} -> {nuevo}")
            conv += 1
            continue
        try:
            scheduler.update_schedule(
                Name=nombre, GroupName=GRUPO,
                ScheduleExpression=nuevo, ScheduleExpressionTimezone=TZ_ESPERADA,
                FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
                State=s['State'],
                Description=((s.get('Description') or '') + ' ' + MARCA).strip())
            print(f"  ✅ {nombre}: {viejo} -> {nuevo}")
            conv += 1
        except ClientError as e:
            print(f"  ❌ {nombre}: {e.response['Error']['Code']}")
            err += 1

    print(f"\nResumen: {'convertiria' if args.dry_run else 'convertidos'} {conv}, "
          f"apartados {apart}, ya-marcados {salt_marca}, "
          f"no-Santiago {salt_tz}, no-en-AWS {salt_nomap}, errores {err}")
    if apartados:
        print("\n⚠️  APARTADOS (rango/lista/underflow — requieren criterio de negocio):")
        for n, v, m in apartados:
            print(f"    {n}: {v}  [{m}]")


if __name__ == '__main__':
    main()
