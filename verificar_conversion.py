"""
=============================================================================
VERIFICAR CONVERSION DE CRONS — barrido completo sobre AWS (no usa CSVs)
=============================================================================

Proposito:
  Tras convertir los crons UTC -> hora local (ver convertir_cron_*.py), este
  script recorre TODOS los schedules del grupo 'default' en EventBridge
  Scheduler y reporta su estado, para confirmar que no quedo ninguno sin
  convertir escondido en cualquier job (no solo redshift-to-lake).

Como decide el estado de cada schedule:
  - convertido : timezone America/Santiago y Description contiene '[cron-local]'
  - pendiente  : timezone America/Santiago pero SIN '[cron-local]'  <-- revisar
  - rate/otro  : la expresion no es cron( ) (rate/at) -> se informa aparte
  - utc/otro-tz: timezone distinto de America/Santiago -> se informa aparte

Los 'pendiente' son los que hay que mirar: o son apartados legitimos
(rango/lista/underflow que cruza medianoche) o realmente se escaparon.
El script marca cuales son de HORA SIMPLE (deberian haberse convertido solos)
vs los que tienen rango/lista (apartados esperados que requieren criterio).

Uso:
  python verificar_conversion.py --region us-east-1
  python verificar_conversion.py --region us-east-1 --grupo default
=============================================================================
"""
import argparse
import re

MARCA = '[cron-local]'
TZ_ESPERADA = 'America/Santiago'


def campos_cron(expr):
    m = re.match(r'^cron\((.+)\)$', (expr or '').strip())
    if not m:
        return None
    campos = m.group(1).split()
    return campos if len(campos) == 6 else None


def clasificar_hora(expr):
    """Devuelve 'simple' | 'rango-lista' | None (no cron)."""
    campos = campos_cron(expr)
    if not campos:
        return None
    hora = campos[1]
    if re.match(r'^\d{1,2}$', hora):
        return 'simple'
    return 'rango-lista'


def listar_schedules(scheduler, grupo):
    out = []
    tok = None
    while True:
        kw = {'GroupName': grupo, 'MaxResults': 100}
        if tok:
            kw['NextToken'] = tok
        resp = scheduler.list_schedules(**kw)
        out.extend(resp.get('Schedules', []))
        tok = resp.get('NextToken')
        if not tok:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--region', default='us-east-1')
    ap.add_argument('--grupo', default='default')
    args = ap.parse_args()

    import boto3
    scheduler = boto3.client('scheduler', region_name=args.region)

    resumen = listar_schedules(scheduler, args.grupo)
    print(f"Schedules en grupo '{args.grupo}': {len(resumen)}\n")

    convertidos = 0
    pendientes_simples = []   # ⚠️ hora simple sin marca -> deberian estar convertidos
    pendientes_apartados = []  # rango/lista sin marca -> apartados esperados
    no_cron = []               # rate()/at()
    otro_tz = []               # timezone != America/Santiago

    for s in resumen:
        nombre = s['Name']
        # list_schedules no trae Description completa ni Target; pedimos el detalle
        d = scheduler.get_schedule(Name=nombre, GroupName=args.grupo)
        expr = d.get('ScheduleExpression', '')
        tz = d.get('ScheduleExpressionTimezone')
        desc = d.get('Description') or ''

        if tz != TZ_ESPERADA:
            otro_tz.append((nombre, expr, tz))
            continue

        tipo = clasificar_hora(expr)
        if tipo is None:
            no_cron.append((nombre, expr))
            continue

        if MARCA in desc:
            convertidos += 1
        else:
            if tipo == 'simple':
                pendientes_simples.append((nombre, expr))
            else:
                pendientes_apartados.append((nombre, expr))

    print("=== RESUMEN ===")
    print(f"  convertidos (Santiago + {MARCA})        : {convertidos}")
    print(f"  PENDIENTES hora simple (revisar!)       : {len(pendientes_simples)}")
    print(f"  pendientes rango/lista (apartados)      : {len(pendientes_apartados)}")
    print(f"  no-cron (rate/at)                       : {len(no_cron)}")
    print(f"  otro timezone (no Santiago)             : {len(otro_tz)}")

    if pendientes_simples:
        print("\n⚠️  HORA SIMPLE SIN CONVERTIR (deberian estar convertidos):")
        for n, e in sorted(pendientes_simples):
            print(f"    {n} | {e}")

    if pendientes_apartados:
        print("\n🔸 RANGO/LISTA SIN MARCA (apartados — requieren criterio):")
        for n, e in sorted(pendientes_apartados):
            print(f"    {n} | {e}")

    if otro_tz:
        print("\nℹ️  TIMEZONE DISTINTO A SANTIAGO:")
        for n, e, tz in sorted(otro_tz):
            print(f"    {n} | {e} | {tz}")


if __name__ == '__main__':
    main()
