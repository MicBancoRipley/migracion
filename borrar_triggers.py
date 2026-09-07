"""
=============================================================================
BORRAR TRIGGERS (los marcados 'borrar' por Bastian) — CON RESPALDO OBLIGATORIO
=============================================================================

Elimina Glue triggers que el negocio marcó como obsoletos. ANTES de borrar cada
uno, guarda su definición completa en respaldos_borrar/<nombre>.json (red de
seguridad: si hubo un error, se puede recrear con restaurar_trigger.py).

Seguridad:
  - Lee los nombres desde el CSV de decisiones (Decision == 'borrar').
  - SIEMPRE respalda antes de borrar (no se puede borrar sin respaldo OK).
  - Separa por estado: los ACTIVATED requieren --incluir-activos explícito
    (borrarlos detiene un proceso vivo).
  - --dry-run para ver qué haría sin borrar nada.

Uso:
    python borrar_triggers.py --dry-run --region us-east-1        # ver plan
    python borrar_triggers.py --region us-east-1                  # borra SOLO seguros (CREATED/DEACTIVATED)
    python borrar_triggers.py --incluir-activos --region us-east-1 # borra tambien los ACTIVATED
"""
import argparse
import csv
import json
import os

ENTRADA = 'triggers_faltantes_decision.csv'
CARPETA_RESP = 'respaldos_borrar'
SEGUROS = {'CREATED', 'DEACTIVATED'}


def nombres_a_borrar(ruta):
    out = []
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f, delimiter=';'):
            dec = (r.get('Decision') or r.get('Decisión') or '').strip().lower()
            if dec == 'borrar':
                out.append(r['trigger_name'].strip())
    return out


def respaldar(glue, nombre):
    """Guarda la definición completa antes de borrar. Devuelve True si OK."""
    from botocore.exceptions import ClientError
    os.makedirs(CARPETA_RESP, exist_ok=True)
    try:
        t = glue.get_trigger(Name=nombre)['Trigger']
    except ClientError as e:
        print(f"    ⚠️  no existe / no se pudo leer ({e.response['Error']['Code']})")
        return None
    ruta = os.path.join(CARPETA_RESP, nombre + '.json')
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(t, f, default=str, indent=2, ensure_ascii=False)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--entrada', default=ENTRADA)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--incluir-activos', action='store_true',
                    help='También borrar los ACTIVATED (detiene procesos vivos)')
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()

    nombres = nombres_a_borrar(args.entrada)
    print(f"Marcados 'borrar' por Bastian: {len(nombres)}")

    import boto3
    from botocore.exceptions import ClientError
    glue = boto3.client('glue', region_name=args.region)

    borrados = respaldados = saltados_activos = errores = 0
    for n in nombres:
        t = respaldar(glue, n)
        if t is None:
            errores += 1
            continue
        respaldados += 1
        estado = t.get('State')

        if estado == 'ACTIVATED' and not args.incluir_activos:
            print(f"  ⏸️  ACTIVO, se salta (usa --incluir-activos): {n}")
            saltados_activos += 1
            continue

        if args.dry_run:
            print(f"  [DRY] borraría [{estado}] {n}  (respaldo OK)")
            continue

        try:
            glue.delete_trigger(Name=n)
            print(f"  🗑️  borrado [{estado}]: {n}")
            borrados += 1
        except ClientError as e:
            print(f"  ❌ error al borrar {n}: {e.response['Error']['Code']}")
            errores += 1

    print(f"\nResumen: respaldados {respaldados}, "
          f"{'borrarían' if args.dry_run else 'borrados'} {borrados}, "
          f"activos saltados {saltados_activos}, errores {errores}")
    if saltados_activos and not args.incluir_activos:
        print("  (los ACTIVATED no se tocaron; corre con --incluir-activos cuando Bastian confirme)")


if __name__ == '__main__':
    main()
