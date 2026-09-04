"""
Analiza el reporte de auditoría del jefe (reporte-migracion-triggers-vs-eventbridge.csv)
y extrae los casos de CHOQUE: un mismo --sql_file_key apuntado por >1 trigger.

Estos son errores de CONFIGURACIÓN preexistentes (no causados por la migración):
varios Glue triggers ejecutan el mismo SQL. Al migrar uno, los otros siguen
activos -> doble/triple ejecución.

Uso:
    python analizar_choques.py --csv reporte-migracion-triggers-vs-eventbridge.csv
"""
import csv
import re
import argparse


def parse_triggers(celda):
    """La columna 'triggers' trae 1+ triggers separados por ' | ', cada uno:
       'nombre [ESTADO] cron(...)'. Devuelve lista de (nombre, estado, cron)."""
    out = []
    if not celda:
        return out
    for parte in celda.split(' | '):
        parte = parte.strip()
        m = re.match(r'^(.*?)\s*\[(\w+)\]\s*(cron\(.*\))?', parte)
        if m:
            out.append((m.group(1).strip(), m.group(2), (m.group(3) or '').strip()))
        elif parte:
            out.append((parte, '?', ''))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='reporte-migracion-triggers-vs-eventbridge.csv')
    args = ap.parse_args()

    choques = []
    with open(args.csv, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            trigs = parse_triggers(row.get('triggers', ''))
            if len(trigs) > 1:
                # activos = los que aún disparan (ACTIVATED)
                activos = [t for t in trigs if t[1] == 'ACTIVATED']
                choques.append({
                    'sql': row.get('sql_file_key', ''),
                    'estado': row.get('estado', ''),
                    'n_triggers': len(trigs),
                    'n_activos': len(activos),
                    'triggers': trigs,
                    'runs_trigger_30d': row.get('runs_trigger_30d', ''),
                })

    # Ordenar: primero los que tienen MÁS de un trigger ACTIVATED (doble disparo real)
    choques.sort(key=lambda c: (-c['n_activos'], -c['n_triggers']))

    print(f"\n{'='*78}")
    print(f"CHOQUES DE --sql_file_key  (varios triggers -> mismo SQL)")
    print(f"{'='*78}")
    print(f"Total grupos con choque: {len(choques)}\n")

    criticos = [c for c in choques if c['n_activos'] >= 2]
    print(f"🔴 CRÍTICOS (2+ triggers ACTIVATED = doble ejecución AHORA): {len(criticos)}")
    print(f"{'-'*78}")
    for c in criticos:
        print(f"\nSQL: {c['sql']}")
        print(f"    estado auditoría: {c['estado']}   runs_trigger_30d: {c['runs_trigger_30d']}")
        for n, st, cron in c['triggers']:
            marca = '⚠️ ACTIVO' if st == 'ACTIVATED' else st
            print(f"    [{marca:12}] {n}  {cron}")

    otros = [c for c in choques if c['n_activos'] < 2]
    print(f"\n\n🟡 REVISAR (1 o 0 activos, pero SQL compartido): {len(otros)}")
    print(f"{'-'*78}")
    for c in otros:
        print(f"\nSQL: {c['sql']}  ({c['estado']})")
        for n, st, cron in c['triggers']:
            print(f"    [{st:12}] {n}  {cron}")


if __name__ == '__main__':
    main()
