"""
=============================================================================
RESPALDAR TRIGGERS (el "seguro" antes de migrar)
=============================================================================

ANTES de tocar un trigger, guarda su definición COMPLETA en un archivo JSON.
Así, si algo falla después de migrar, puedes RECREAR el trigger idéntico con
restaurar_trigger.py (el botón de deshacer).

Por qué es imprescindible en la migración masiva:
    - El flujo seguro apaga el trigger viejo (switch) y días después lo elimina.
    - Si el schedule nuevo falla DESPUÉS de eliminar el trigger, sin respaldo
      no habría forma de volver atrás. Con el respaldo, lo recreas en segundos.

Qué guarda (todo lo necesario para recrearlo con create_trigger):
    Name, Type, Schedule, Actions (con Arguments, Timeout, SecurityConfiguration,
    NotificationProperty), Description, Predicate, StartOnCreation, WorkflowName,
    y el State original (ACTIVATED/DEACTIVATED) para saber si iba activo.

Dónde: un archivo por trigger en la carpeta  respaldos/<nombre>.json
    Además un índice  respaldos/_indice.json  con el resumen.

USO:
    # Respaldar TODOS los triggers del CSV de control (recomendado antes de migrar):
    python respaldar_triggers.py --region us-east-1

    # Respaldar solo algunos (por nombre):
    python respaldar_triggers.py --trigger sdlf-bigdata-abr-historico-glue-trigger --region us-east-1

    # Respaldar los que apuntan a un job (útil para esta migración):
    python respaldar_triggers.py --job sdlf-bigdata-redshift-segmentation-schedule-glue-job --region us-east-1

    # Re-respaldar aunque ya exista el archivo:
    python respaldar_triggers.py --region us-east-1 --forzar

    # Practicar con moto:
    python respaldar_triggers.py --demo

=============================================================================
"""

import os
import csv
import json
import argparse
import datetime

CARPETA_RESPALDOS = 'respaldos'
ARCHIVO_CONTROL = 'control_migracion.csv'


def _serializable(obj):
    """Convierte datetimes a string para poder guardar en JSON."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f'No serializable: {type(obj)}')


def extraer_definicion(trigger):
    """Extrae de un Trigger (get_trigger) solo lo necesario para recrearlo.

    Guardamos también el State original para saber si estaba ACTIVATED.
    """
    definicion = {
        'Name': trigger.get('Name'),
        'Type': trigger.get('Type'),
        'State': trigger.get('State'),  # ACTIVATED / DEACTIVATED / CREATED
        'Schedule': trigger.get('Schedule'),
        'Actions': trigger.get('Actions', []),
        'Description': trigger.get('Description', ''),
        'Predicate': trigger.get('Predicate'),          # para CONDITIONAL
        'WorkflowName': trigger.get('WorkflowName'),
        'EventBatchingCondition': trigger.get('EventBatchingCondition'),
        '_respaldado_en': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    # Quitar claves None para un JSON limpio
    return {k: v for k, v in definicion.items() if v is not None}


def ruta_respaldo(nombre):
    return os.path.join(CARPETA_RESPALDOS, nombre + '.json')


def ya_respaldado(nombre):
    return os.path.exists(ruta_respaldo(nombre))


def guardar_respaldo(definicion):
    os.makedirs(CARPETA_RESPALDOS, exist_ok=True)
    ruta = ruta_respaldo(definicion['Name'])
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(definicion, f, ensure_ascii=False, indent=2, default=_serializable)
    return ruta


def nombres_desde_control(ruta=ARCHIVO_CONTROL):
    """Lee los trigger_name del CSV de control."""
    if not os.path.exists(ruta):
        return []
    with open(ruta, 'r', encoding='utf-8-sig', newline='') as f:
        return [fila['trigger_name'] for fila in csv.DictReader(f, delimiter=';')]


def listar_por_job(glue, job_objetivo):
    """Recorre todos los triggers y devuelve los que apuntan a ese job."""
    objetivo = job_objetivo.strip()
    nombres = []
    for page in glue.get_paginator('get_triggers').paginate():
        for tr in page.get('Triggers', []):
            acts = tr.get('Actions') or []
            job = (acts[0].get('JobName', '') if acts else '').strip()
            if job == objetivo:
                nombres.append(tr.get('Name'))
    return nombres


def respaldar(glue, nombres, forzar=False):
    """Respalda la lista de triggers. Devuelve (ok, saltados, errores)."""
    from botocore.exceptions import ClientError

    os.makedirs(CARPETA_RESPALDOS, exist_ok=True)
    ok = saltados = errores = 0
    indice = []

    for nombre in nombres:
        if not forzar and ya_respaldado(nombre):
            print(f"  ⏭️  ya respaldado: {nombre}")
            saltados += 1
            indice.append({'trigger': nombre, 'estado': 'ya_existia'})
            continue
        try:
            trigger = glue.get_trigger(Name=nombre)['Trigger']
            definicion = extraer_definicion(trigger)
            ruta = guardar_respaldo(definicion)
            print(f"  ✅ respaldado: {nombre}  ({definicion.get('State')})")
            ok += 1
            indice.append({'trigger': nombre, 'estado': 'ok',
                           'state_original': definicion.get('State'),
                           'archivo': ruta})
        except ClientError as e:
            codigo = e.response['Error']['Code']
            print(f"  ❌ error respaldando {nombre}: {codigo}")
            errores += 1
            indice.append({'trigger': nombre, 'estado': 'error', 'codigo': codigo})

    # Guardar índice acumulado
    _guardar_indice(indice)

    print(f"\n  Resumen respaldo: {ok} nuevos, {saltados} ya existían, {errores} errores.")
    print(f"  Carpeta: {CARPETA_RESPALDOS}/")
    if ok or saltados:
        print(f"  👉 Si algo falla tras migrar, restaura con:")
        print(f"     python restaurar_trigger.py --trigger NOMBRE")
    return ok, saltados, errores


def _guardar_indice(entradas_nuevas):
    """Mantiene un índice acumulado de lo respaldado (para tener el panorama)."""
    ruta_idx = os.path.join(CARPETA_RESPALDOS, '_indice.json')
    previo = []
    if os.path.exists(ruta_idx):
        try:
            with open(ruta_idx, encoding='utf-8') as f:
                previo = json.load(f)
        except Exception:
            previo = []
    # Índice por trigger (última entrada gana)
    por_nombre = {e['trigger']: e for e in previo}
    for e in entradas_nuevas:
        e['fecha'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        por_nombre[e['trigger']] = e
    with open(ruta_idx, 'w', encoding='utf-8') as f:
        json.dump(list(por_nombre.values()), f, ensure_ascii=False, indent=2)


def _resolver_nombres(glue, args):
    """Determina qué triggers respaldar según los flags."""
    if args.trigger:
        return [args.trigger]
    if args.job:
        nombres = listar_por_job(glue, args.job)
        print(f"  {len(nombres)} triggers apuntan al job '{args.job}'")
        return nombres
    # Por defecto: los del CSV de control
    nombres = nombres_desde_control()
    if not nombres:
        print(f"  ⚠️  No hay {ARCHIVO_CONTROL} o está vacío, y no diste --trigger/--job.")
    else:
        print(f"  {len(nombres)} triggers en {ARCHIVO_CONTROL}")
    return nombres


def main():
    args = parse_args()

    if args.demo:
        import boto3
        from moto import mock_aws

        @mock_aws
        def _demo():
            glue = boto3.client('glue', region_name=args.region)
            glue.create_job(Name='sdlf-bigdata-redshift-segmentation-schedule-glue-job',
                            Role='arn:aws:iam::111111111111:role/R',
                            Command={'Name': 'glueetl', 'ScriptLocation': 's3://x/j.py', 'PythonVersion': '3'})
            glue.create_trigger(
                Name='sdlf-bigdata-demo-negocio-glue-trigger', Type='SCHEDULED',
                Schedule='cron(00 13 * * ? *)', Description='trigger de negocio demo',
                Actions=[{'JobName': 'sdlf-bigdata-redshift-segmentation-schedule-glue-job',
                          'Arguments': {'--sql_file_key': 'redshift-sql-scheduled/demo.sql',
                                        '--vs_notification': 'clientes'},
                          'SecurityConfiguration': 'sdlf-bigdata-glue-security-config'}])
            print("### DEMO: respaldando por --job ###")
            nombres = listar_por_job(glue, 'sdlf-bigdata-redshift-segmentation-schedule-glue-job')
            respaldar(glue, nombres, forzar=args.forzar)
            # Mostrar el contenido del respaldo generado
            ruta = ruta_respaldo('sdlf-bigdata-demo-negocio-glue-trigger')
            print(f"\n### Contenido de {ruta}: ###")
            with open(ruta, encoding='utf-8') as f:
                print(f.read())
        _demo()
        return

    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError
    try:
        glue = boto3.client('glue', region_name=args.region)
        nombres = _resolver_nombres(glue, args)
        if nombres:
            respaldar(glue, nombres, forzar=args.forzar)
    except NoCredentialsError:
        print("❌ No hay credenciales AWS. Configúralas o usa --demo.")
    except ClientError as e:
        print(f"❌ Error de AWS: {e}")


def parse_args():
    p = argparse.ArgumentParser(description='Respalda la definición de Glue triggers antes de migrar')
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--trigger', help='Respaldar solo este trigger')
    p.add_argument('--job', help='Respaldar los triggers que apuntan a este job')
    p.add_argument('--forzar', action='store_true', help='Re-respaldar aunque ya exista el archivo')
    p.add_argument('--demo', action='store_true', help='Practicar con moto')
    return p.parse_args()


if __name__ == '__main__':
    main()
