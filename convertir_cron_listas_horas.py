"""
=============================================================================
CONVERTIR CRON con LISTA DE HORAS: UTC -> hora local (resta offset a c/hora)
=============================================================================

Para schedules cuyo campo HORA es una LISTA de horas concretas separadas por
coma, p.ej. cron(0 1,14,19 * * ? *). El conversor de hora simple los apartaba
porque no son un solo numero. Este resta el offset a CADA hora de la lista.

Ejemplos (offset 3):
    cron(0 1,14,19 ...)  -> 1->22(wrap) 14->11 19->16  =>  cron(0 22,11,16 ...)
    cron(0 11,13,15,17 ...) -> 8,10,12,14              =>  cron(0 8,10,12,14 ...)
    cron(0 3,15 ...)     -> 0,12                        =>  cron(0 0,12 ...)

SOLO maneja LISTAS de numeros (con comas). NO toca:
    - rangos con guion (12-0, 0-7)  -> los aparta (motivo 'rango')
    - steps (*/3, 0/1)              -> los aparta (motivo 'step')
    - hora simple (un solo numero)  -> usa convertir_cron_pendientes.py

Nota de negocio (Bastian): estos ya corrian en su hora UTC fisica; restar el
offset los devuelve a esa hora real. El wrap de una hora de madrugada a la
noche del dia anterior es cosmetico (corren en el mismo instante UTC de siempre).

SEGURIDAD:
    - Solo toca schedules cuyo timezone ES America/Santiago.
    - Idempotente: marca [cron-local] y salta los ya marcados.
    - --dry-run para revisar.

Uso:
    python convertir_cron_listas_horas.py --lista listas.txt --offset 3 \
        --dry-run --region us-east-1
=============================================================================
"""
import argparse
import re

GRUPO = 'default'
TZ_ESPERADA = 'America/Santiago'
MARCA = '[cron-local]'


def cargar_lista(ruta):
    nombres = []
    for linea in open(ruta, encoding='utf-8'):
        s = linea.strip()
        if not s or s.startswith('#'):
            continue
        s = re.split(r'\s*\|\s*|\s+', s)[0]
        if s:
            nombres.append(s)
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


def convertir_lista_horas(expr, offset):
    """Convierte el campo hora si es una LISTA de numeros (con comas).
    Devuelve (nuevo_cron, None) o (None, motivo)."""
    campos = parse_cron(expr)
    if not campos:
        return None, 'no-parseable'
    hora = campos[1]

    if '/' in hora:
        return None, f'step ({hora})'
    if '-' in hora:
        return None, f'rango ({hora})'
    if ',' not in hora:
        return None, f'no es lista ({hora})'   # hora simple -> otro script

    partes = hora.split(',')
    nuevas = []
    for p in partes:
        if not re.match(r'^\d{1,2}$', p):
            return None, f'elemento no numerico ({p})'
        n = int(p) - offset
        if n < 0:
            n += 24          # wrap inofensivo (aprobado negocio)
        if n > 23:
            return None, f'overflow ({p}-{offset}={int(p)-offset})'
        nuevas.append(str(n))

    campos[1] = ','.join(nuevas)
    return f"cron({' '.join(campos)})", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lista', required=True, help='Archivo .txt con nombres de schedule')
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
        nuevo, motivo = convertir_lista_horas(viejo, args.offset)
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
        print("\n⚠️  APARTADOS (rango/step/otro — requieren criterio):")
        for n, v, m in apartados:
            print(f"    {n}: {v}  [{m}]")


if __name__ == '__main__':
    main()
