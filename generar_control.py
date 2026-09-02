"""
=============================================================================
GENERAR ARCHIVO DE CONTROL (el "cerebro" de la migración por lotes)
=============================================================================

Este script lee TODOS tus Glue triggers, los filtra, y produce un archivo CSV
que es la FUENTE DE VERDAD de la migración masiva:

    control_migracion.csv

Cada fila = un trigger. La columna 'estado' dice en qué punto está:

    pendiente   -> aún no se ha tocado (listo para crear el schedule)
    creado      -> ya se creó el schedule DESACTIVADO (falta verificar/switch)
    verificado  -> el schedule creado coincide con el trigger (listo para switch)
    migrado     -> switch hecho: trigger apagado + schedule activo
    revisar     -> caso especial (multi-job, conditional, etc.) -> NO automático
    error       -> algo falló (ver columna 'nota')

¿Por qué un CSV?
    - Tu jefe lo abre en Excel y ve el avance de un vistazo.
    - Es la memoria del proceso: si el script se cae, retoma leyendo este archivo.
    - Es idempotente: solo se procesan las filas cuyo estado lo permite.

USO:
    # Real (lee tus triggers de AWS):
    python generar_control.py --region us-east-1

    # Práctica con moto (crea triggers falsos):
    python generar_control.py --demo

    # Regenerar SIN perder el avance ya registrado (fusiona estados):
    python generar_control.py --region us-east-1 --fusionar

=============================================================================
"""

import csv
import os
import argparse

PREFIJO = 'sdlf-bigdata'
ARCHIVO_CONTROL = 'control_migracion.csv'

# Columnas del CSV (orden fijo)
COLUMNAS = [
    'trigger_name',    # nombre del Glue trigger original
    'schedule_name',   # nombre que tendrá el EventBridge Schedule
    'cron',            # expresión de agenda (para revisar de un vistazo)
    'job_name',        # job que dispara (el primero, si hay varios)
    'estado',          # pendiente | creado | verificado | migrado | revisar | error
    'nota',            # motivo si es 'revisar' o 'error'
    'actualizado',     # timestamp de la última vez que cambió esta fila
]

ESTADO_INICIAL = 'pendiente'


def nombre_schedule_desde_trigger(trigger_name):
    """Mismo criterio que migrar_seguro.py: -trigger -> -schedule."""
    if trigger_name.endswith('-trigger'):
        return trigger_name[:-len('-trigger')] + '-schedule'
    return trigger_name + '-schedule'


def clasificar_trigger(trigger):
    """Decide si un trigger es migrable automáticamente o hay que revisarlo.

    Devuelve (estado, nota). estado = 'pendiente' (migrable) o 'revisar'.
    """
    import reglas_exclusion

    nombre = trigger.get('Name', '')
    actions = trigger.get('Actions', [])
    primer_job = actions[0].get('JobName', '') if actions else ''

    # 0. REGLA DEL EQUIPO: excluir BI (bigdata-bi-) y matinal (nombre o job).
    #    Estos NO se migran automáticamente -> quedan como 'revisar'.
    motivo = reglas_exclusion.motivo_exclusion(nombre, primer_job)
    if motivo:
        return 'revisar', motivo

    tipo = trigger.get('Type')

    # 1. Solo los SCHEDULED van al flujo automático.
    if tipo != 'SCHEDULED':
        return 'revisar', f'Tipo {tipo}: no es SCHEDULED (usar Rules/Step Functions o es manual)'

    # 2. Debe tener un cron.
    if not trigger.get('Schedule'):
        return 'revisar', 'SCHEDULED sin expresión de agenda'

    actions = trigger.get('Actions', [])
    if not actions:
        return 'revisar', 'Sin Actions (no dispara ningún job)'

    # 3. Múltiples acciones -> necesita 1 schedule por job o Step Functions.
    if len(actions) > 1:
        return 'revisar', f'{len(actions)} acciones (multi-job): requiere estrategia especial'

    primer = actions[0]

    # 4. El action debe apuntar a un JobName (no a un crawler).
    job = primer.get('JobName')
    if not job:
        if primer.get('CrawlerName'):
            return 'revisar', 'Action apunta a un Crawler, no a un Job'
        return 'revisar', 'Action sin JobName'

    # 5. Nombre de job con caracteres raros (tab/espacios) -> el bug que ya vimos.
    if job != job.strip() or '\t' in job:
        return 'revisar', f'JobName con caracteres invisibles/tab: {repr(job)}'

    return 'pendiente', ''


def construir_filas(glue, job_filtro=None):
    """Recorre TODOS los triggers, filtra por prefijo y arma las filas del CSV.

    Si job_filtro se pasa, SOLO entran los triggers cuyo Actions[0].JobName
    coincide EXACTAMENTE con ese job (el criterio correcto: filtrar por el
    JobName REAL, no por el nombre del trigger). Así atrapa triggers que
    apuntan al job aunque se llamen distinto, y descarta los que se llaman
    parecido pero apuntan a otro job.
    """
    filas = []
    paginator = glue.get_paginator('get_triggers')
    total = 0
    ignorados_prefijo = 0
    ignorados_otro_job = 0

    # Normalizamos el job objetivo (quita tab/espacios del bug conocido).
    job_objetivo = job_filtro.strip() if job_filtro else None

    for page in paginator.paginate():
        for trigger in page.get('Triggers', []):
            total += 1
            nombre = trigger.get('Name', '')

            # Solo los de nuestro proyecto.
            if not nombre.startswith(PREFIJO):
                ignorados_prefijo += 1
                continue

            actions = trigger.get('Actions', [])
            job = actions[0].get('JobName', '') if actions else ''

            # FILTRO POR JOB REAL: si se pidió, solo los que apuntan a ese job.
            # Comparamos normalizando espacios/tab para no perder los que traen
            # el JobName "sucio" (ej. "\tsdlf-bigdata-...").
            if job_objetivo is not None and (job or '').strip() != job_objetivo:
                ignorados_otro_job += 1
                continue

            estado, nota = clasificar_trigger(trigger)

            filas.append({
                'trigger_name': nombre,
                'schedule_name': nombre_schedule_desde_trigger(nombre),
                'cron': trigger.get('Schedule', ''),
                'job_name': job,
                'estado': estado,
                'nota': nota,
                'actualizado': '',
            })

    print(f"  Triggers totales en la cuenta: {total}")
    print(f"  Ignorados (otro prefijo):      {ignorados_prefijo}")
    if job_objetivo is not None:
        print(f"  Ignorados (otro job):          {ignorados_otro_job}")
        print(f"  Apuntan a '{job_objetivo}': {len(filas)}")
    else:
        print(f"  De prefijo '{PREFIJO}':         {len(filas)}")
    return filas


def leer_control_existente(ruta):
    """Lee el CSV existente y devuelve dict {trigger_name: fila} para fusionar."""
    if not os.path.exists(ruta):
        return {}
    existentes = {}
    with open(ruta, 'r', encoding='utf-8-sig', newline='') as f:
        lector = csv.DictReader(f, delimiter=';')
        for fila in lector:
            existentes[fila['trigger_name']] = fila
    return existentes


def fusionar(filas_nuevas, existentes):
    """Conserva el estado ya registrado para triggers que ya estaban en el CSV.

    Regla: si un trigger ya tenía un estado de avance (creado/verificado/migrado),
    NO lo pisamos con 'pendiente'. Así no perdemos el progreso al regenerar.
    """
    estados_de_avance = {'creado', 'verificado', 'migrado', 'error', 'revisar'}
    conservados = 0
    for fila in filas_nuevas:
        previa = existentes.get(fila['trigger_name'])
        if previa and previa.get('estado') in estados_de_avance:
            fila['estado'] = previa['estado']
            fila['nota'] = previa.get('nota', fila['nota'])
            fila['actualizado'] = previa.get('actualizado', '')
            conservados += 1
    if conservados:
        print(f"  Fusión: se conservó el estado de {conservados} triggers ya registrados.")
    return filas_nuevas


def escribir_control(filas, ruta):
    """Escribe el CSV con ; y utf-8-sig (para que Excel lo abra bien)."""
    with open(ruta, 'w', encoding='utf-8-sig', newline='') as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS, delimiter=';')
        escritor.writeheader()
        escritor.writerows(filas)


def resumen(filas):
    """Imprime un resumen por estado."""
    from collections import Counter
    conteo = Counter(f['estado'] for f in filas)
    print("\n  --- Resumen del archivo de control ---")
    for estado in ['pendiente', 'creado', 'verificado', 'migrado', 'revisar', 'error']:
        if conteo.get(estado):
            print(f"    {estado:<12} {conteo[estado]}")
    total_migrable = conteo.get('pendiente', 0) + conteo.get('creado', 0) + \
        conteo.get('verificado', 0) + conteo.get('migrado', 0)
    print(f"    {'-'*24}")
    print(f"    migrables automáticos: {total_migrable}")
    print(f"    a revisar manualmente: {conteo.get('revisar', 0)}")


def generar(glue, fusionar_estados=False, job_filtro=None):
    print("Leyendo triggers y construyendo el archivo de control...\n")
    if job_filtro:
        print(f"  Filtro: SOLO triggers que apuntan al job '{job_filtro}'\n")
    filas = construir_filas(glue, job_filtro=job_filtro)

    if not filas:
        if job_filtro:
            print(f"\n  ⚠️  Ningún trigger del prefijo apunta al job '{job_filtro}'. Nada que escribir.")
        else:
            print("\n  ⚠️  No se encontraron triggers con el prefijo. Nada que escribir.")
        return

    if fusionar_estados:
        existentes = leer_control_existente(ARCHIVO_CONTROL)
        if existentes:
            filas = fusionar(filas, existentes)

    # Orden útil: primero los pendientes, luego el resto; alfabético dentro.
    orden_estado = {'pendiente': 0, 'creado': 1, 'verificado': 2, 'migrado': 3,
                    'error': 4, 'revisar': 5}
    filas.sort(key=lambda f: (orden_estado.get(f['estado'], 9), f['trigger_name']))

    escribir_control(filas, ARCHIVO_CONTROL)
    resumen(filas)
    print(f"\n  ✅ Escrito: {ARCHIVO_CONTROL}  ({len(filas)} filas)")
    print(f"     Ábrelo en Excel para revisarlo con tu jefe.")
    print(f"\n  👉 Siguiente: crear los schedules en lote (todos DESACTIVADOS):")
    print(f"     python migrar_lote.py --paso crear-lote --limit 5 --dry-run")


def main():
    args = parse_args()

    if args.demo:
        import boto3
        from moto import mock_aws

        @mock_aws
        def _demo():
            glue = boto3.client('glue', region_name=args.region)
            # Creamos triggers de prueba variados para ver la clasificación.
            for i in range(1, 4):
                glue.create_job(Name=f'sdlf-bigdata-job-{i}', Role='arn:aws:iam::111111111111:role/R',
                                Command={'Name': 'glueetl', 'ScriptLocation': 's3://x/j.py', 'PythonVersion': '3'})
            # 3 migrables
            for i in range(1, 4):
                glue.create_trigger(Name=f'sdlf-bigdata-ok-{i}-glue-trigger', Type='SCHEDULED',
                                    Schedule=f'cron(0 {i} * * ? *)',
                                    Actions=[{'JobName': f'sdlf-bigdata-job-{i}'}])
            # 1 multi-job (revisar)
            glue.create_trigger(Name='sdlf-bigdata-multi-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 5 * * ? *)',
                                Actions=[{'JobName': 'sdlf-bigdata-job-1'},
                                         {'JobName': 'sdlf-bigdata-job-2'}])
            # 1 conditional (revisar)
            glue.create_trigger(Name='sdlf-bigdata-cond-glue-trigger', Type='CONDITIONAL',
                                Predicate={'Logical': 'AND', 'Conditions': [
                                    {'LogicalOperator': 'EQUALS', 'JobName': 'sdlf-bigdata-job-1',
                                     'State': 'SUCCEEDED'}]},
                                Actions=[{'JobName': 'sdlf-bigdata-job-2'}])
            # 1 de otro prefijo (se ignora)
            glue.create_trigger(Name='otro-proyecto-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 6 * * ? *)',
                                Actions=[{'JobName': 'sdlf-bigdata-job-1'}])

            generar(glue, fusionar_estados=args.fusionar, job_filtro=args.job)

        _demo()
        return

    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError
    try:
        glue = boto3.client('glue', region_name=args.region)
        generar(glue, fusionar_estados=args.fusionar, job_filtro=args.job)
    except NoCredentialsError:
        print("❌ No hay credenciales AWS. Configúralas o usa --demo.")
    except ClientError as e:
        print(f"❌ Error de AWS: {e}")


def parse_args():
    p = argparse.ArgumentParser(description='Genera el archivo de control CSV para la migración por lotes')
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--demo', action='store_true', help='Practicar con triggers falsos (moto)')
    p.add_argument('--fusionar', action='store_true',
                   help='Conservar el avance ya registrado al regenerar (no pisar estados)')
    p.add_argument('--job',
                   help='Solo incluir triggers cuyo Actions[0].JobName sea EXACTAMENTE este job')
    return p.parse_args()


if __name__ == '__main__':
    main()
