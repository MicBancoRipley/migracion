"""
=============================================================================
CAMBIAR TIMEZONE DE SCHEDULES: UTC -> America/Santiago
=============================================================================

Los schedules se migraron con ScheduleExpressionTimezone='UTC'. El negocio los
quiere en hora de Chile (America/Santiago), que ademas ajusta automaticamente
el horario de verano/invierno (evita el desfase del cambio de hora).

Este script recorre los schedules migrados (prefijo sdlf-bigdata, o los del
control CSV) y hace update_schedule cambiando SOLO el timezone. El cron, job,
argumentos, estado (ENABLED/DISABLED) y todo lo demas se mantiene IDENTICO.

⚠️ OJO: cambiar el timezone MUEVE la hora UTC real de disparo (porque ahora
'10:15' se interpreta como 10:15 Chile, no 10:15 UTC). Eso es lo deseado:
devuelve cada proceso a su hora local. Verifica con --dry-run primero.

Uso:
    python retimezone_lote.py --dry-run --region us-east-1       # ver que haria
    python retimezone_lote.py --limit 20 --region us-east-1      # aplicar en lote
    python retimezone_lote.py --region us-east-1                 # aplicar a todos

Solo toca schedules que HOY esten en UTC (idempotente: si ya estan en
America/Santiago, los salta).
=============================================================================
"""
import argparse
import json
import time

PREFIJO = 'sdlf-bigdata'
TZ_ORIGEN = 'UTC'
TZ_DESTINO = 'America/Santiago'
PAUSA = 0.2


def listar_schedules(scheduler):
    """Todos los schedules del proyecto (paginado)."""
    nombres = []
    token = None
    while True:
        kwargs = {'MaxResults': 100}
        if token:
            kwargs['NextToken'] = token
        resp = scheduler.list_schedules(**kwargs)
        for s in resp.get('Schedules', []):
            n = s['Name']
            if n.startswith(PREFIJO) or n.startswith('sp_'):
                nombres.append(n)
        token = resp.get('NextToken')
        if not token:
            break
    return nombres


def _hora_utc_equivalente(cron_expr, tz):
    """Solo informativo: describe el cambio. No calcula DST real (eso lo hace AWS)."""
    return f"{cron_expr}  [{tz}]"


def procesar(scheduler, dry_run, limit):
    from botocore.exceptions import ClientError
    nombres = listar_schedules(scheduler)
    print(f"Schedules del proyecto encontrados: {len(nombres)}")

    cambiados = saltados = errores = 0
    for nombre in nombres:
        if limit and cambiados >= limit:
            print(f"\n(limit {limit} alcanzado)")
            break
        try:
            s = scheduler.get_schedule(Name=nombre)
        except ClientError as e:
            print(f"  ⚠️  no se pudo leer {nombre}: {e.response['Error']['Code']}")
            errores += 1
            continue

        tz_actual = s.get('ScheduleExpressionTimezone', 'UTC')
        if tz_actual == TZ_DESTINO:
            saltados += 1
            continue  # ya esta en Chile, idempotente
        if tz_actual != TZ_ORIGEN:
            print(f"  ⏭️  {nombre}: tz inesperada '{tz_actual}', se salta (revisar a mano)")
            saltados += 1
            continue

        cron = s['ScheduleExpression']
        if dry_run:
            print(f"  [DRY] {nombre}")
            print(f"        {cron}  {tz_actual} -> {TZ_DESTINO}")
            cambiados += 1
            continue

        try:
            scheduler.update_schedule(
                Name=nombre,
                ScheduleExpression=cron,
                ScheduleExpressionTimezone=TZ_DESTINO,   # <-- unico cambio
                FlexibleTimeWindow=s['FlexibleTimeWindow'],
                Target=s['Target'],
                State=s['State'],                        # respeta ENABLED/DISABLED
                Description=s.get('Description', ''),
            )
            print(f"  ✅ {nombre}: {tz_actual} -> {TZ_DESTINO}")
            cambiados += 1
            time.sleep(PAUSA)
        except ClientError as e:
            print(f"  ❌ error en {nombre}: {e.response['Error']['Code']}")
            errores += 1

    print(f"\nResumen: {cambiados} {'a cambiar (DRY)' if dry_run else 'cambiados'}, "
          f"{saltados} saltados (ya en Chile / otra tz), {errores} errores.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()
    import boto3
    scheduler = boto3.client('scheduler', region_name=args.region)
    procesar(scheduler, args.dry_run, args.limit)


if __name__ == '__main__':
    main()
