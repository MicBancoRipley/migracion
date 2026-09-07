"""
Genera un control_*.csv PARTIENDO de un CSV de decisiones (columna Decision).

El CSV de decisiones marca cada trigger con una columna 'Decision'. Este script
toma SOLO los que dicen 'migrar' y arma un archivo de control con esos exactos,
consultando su definición real en Glue (cron, job, estado). Así migramos
exactamente los aprobados, sin tocar los 'borrar' ni los 'no tocar' aunque
compartan job.

Entrada:  triggers_faltantes_decision.csv  (col: trigger_name;...;Decision)
Salida:   control_a_migrar.csv  (mismo formato que generar_control)

Uso:
    python control_desde_decision.py --region us-east-1
    python control_desde_decision.py --decision migrar --salida control_a_migrar.csv
"""
import argparse
import csv
import json
import datetime

import reglas_exclusion

ENTRADA = 'triggers_faltantes_decision.csv'
CAMPOS = ['trigger_name', 'schedule_name', 'cron', 'job_name', 'estado', 'nota', 'actualizado']


def ahora():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def leer_decision(ruta, decision):
    """Nombres de trigger cuya columna Decision == decision (ej. 'migrar')."""
    out = []
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f, delimiter=';'):
            dec = (r.get('Decision') or r.get('Decisión') or '').strip().lower()
            if dec == decision.lower():
                out.append(r['trigger_name'].strip())
    return out


def construir(glue, nombres):
    """Para cada trigger, lee su definición real y arma la fila de control."""
    from botocore.exceptions import ClientError
    filas = []
    saltados = []
    for n in nombres:
        try:
            t = glue.get_trigger(Name=n)['Trigger']
        except ClientError as e:
            saltados.append((n, e.response['Error']['Code']))
            continue
        if t.get('Type') != 'SCHEDULED' or not t.get('Schedule'):
            saltados.append((n, f"tipo {t.get('Type')} / sin cron"))
            continue
        acts = t.get('Actions', [])
        job = acts[0].get('JobName') if acts else ''
        filas.append({
            'trigger_name': n,
            'schedule_name': reglas_exclusion.nombre_schedule_desde_trigger(n),
            'cron': t.get('Schedule', ''),
            'job_name': job,
            'estado': 'pendiente',
            'nota': 'decision=migrar',
            'actualizado': ahora(),
        })
    return filas, saltados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--entrada', default=ENTRADA)
    ap.add_argument('--decision', default='migrar')
    ap.add_argument('--salida', default='control_a_migrar.csv')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    nombres = leer_decision(args.entrada, args.decision)
    print(f"Triggers con Decision='{args.decision}': {len(nombres)}")

    import boto3
    glue = boto3.client('glue', region_name=args.region)
    filas, saltados = construir(glue, nombres)

    with open(args.salida, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=';')
        w.writeheader()
        w.writerows(filas)

    print(f"✅ Escrito {args.salida}: {len(filas)} filas 'pendiente'")
    if saltados:
        print(f"\n⚠️  {len(saltados)} saltados (no SCHEDULED, sin cron, o no existen):")
        for n, motivo in saltados:
            print(f"    {n}  -> {motivo}")


if __name__ == '__main__':
    main()
