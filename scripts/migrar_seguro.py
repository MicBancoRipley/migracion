"""
=============================================================================
MIGRACIÓN SEGURA DE 1 TRIGGER (paso a paso, controlado)
=============================================================================

Migra UN Glue Trigger a EventBridge Scheduler con el flujo profesional seguro,
para que NUNCA haya doble ejecución ni ventana sin ejecución descontrolada.

FLUJO (cada paso es un comando separado que TÚ ejecutas y verificas):

    paso 1: crear     → crea el schedule DESACTIVADO (no compite con el viejo)
    paso 2: verificar → confirma que el schedule quedó bien configurado
    paso 3: switch    → apaga el Glue trigger Y activa el schedule (seguidos)
    paso 4: estado    → muestra el estado actual de ambos (para monitorear)
    paso 5: limpiar   → (DÍAS DESPUÉS) elimina el Glue trigger viejo

¿Por qué este orden?
    - Crear DESACTIVADO: el nuevo no dispara nada todavía -> cero riesgo
    - Switch = apagar viejo, luego prender nuevo -> preferimos una micro-ventana
      sin ejecución (recuperable) antes que doble ejecución (corrompe datos)
    - Eliminar al final, tras monitorear varios días

⚠️ ANTES DE EMPEZAR:
    - Define el ARN del rol del scheduler en la variable de entorno
      SCHEDULER_ROLE_ARN (o edítalo en la sección CONFIGURACIÓN).
    - Ten credenciales AWS válidas para la cuenta/región objetivo.
    - Revisa el cron del trigger: no hagas el switch si está por ejecutarse pronto.

USO:
    python migrar_seguro.py --trigger NOMBRE-DEL-TRIGGER --paso crear
    python migrar_seguro.py --trigger NOMBRE-DEL-TRIGGER --paso verificar
    python migrar_seguro.py --trigger NOMBRE-DEL-TRIGGER --paso switch
    python migrar_seguro.py --trigger NOMBRE-DEL-TRIGGER --paso estado
    python migrar_seguro.py --trigger NOMBRE-DEL-TRIGGER --paso limpiar

    # Practicar el flujo completo con moto (sin AWS real):
    python migrar_seguro.py --demo --trigger ejemplo-glue-trigger --paso crear
    ... (repite con verificar, switch, estado)

=============================================================================
"""

import os
import json
import argparse

# Reglas que se aplican AL CREAR (timezone, offset de cron y grupo destino).
# Centralizadas en reglas_migracion.py para que migrar_seguro y migrar_lote
# creen los schedules YA correctos (evita el trabajo de reparacion posterior).
import reglas_migracion


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

# ARN del rol que EventBridge Scheduler usa para invocar Glue. Se toma de la
# variable de entorno SCHEDULER_ROLE_ARN; si no está, usa el placeholder de
# abajo (edítalo o exporta la variable antes de correr en real).
SCHEDULER_ROLE_ARN = os.environ.get(
    'SCHEDULER_ROLE_ARN',
    'arn:aws:iam::<ACCOUNT_ID>:role/<SCHEDULER_ROLE_NAME>')
# Timezone de los schedules. America/Santiago (hora de Chile): el negocio piensa
# los horarios en hora local y EventBridge ajusta verano/invierno automaticamente
# (evita el desfase que tuvimos con UTC fijo). El valor real se lee de
# reglas_migracion.timezone_objetivo() (configurable en "Decisiones previas");
# esta constante queda como fallback/compatibilidad.
TIMEZONE = 'America/Santiago'
RETRY_ATTEMPTS = 2
RETRY_MAX_AGE_SECONDS = 3600


def nombre_schedule_desde_trigger(trigger_name):
    """Delega en reglas_exclusion, que aplica la regla de acortado del equipo
    (quitar 'sdlf-bigdata-' y '-glue') cuando el nombre supera 64 chars.
    Así el nombre del schedule es idéntico en todos los scripts del proyecto."""
    try:
        import reglas_exclusion
        return reglas_exclusion.nombre_schedule_desde_trigger(trigger_name)
    except Exception:
        # Fallback al criterio base si no está disponible el módulo.
        if trigger_name.endswith('-trigger'):
            return trigger_name[:-len('-trigger')] + '-schedule'
        return trigger_name + '-schedule'


def grupo_de_trigger(glue, trigger_name):
    """Deduce en qué grupo vive (o vivirá) el schedule de este trigger, según
    la regla de grupos aplicada al job. Los pasos posteriores al crear
    (verificar/switch/estado/rollback) lo necesitan porque en EventBridge
    get_schedule/update_schedule requieren el GroupName correcto (ya no 'default').

    Si algo falla al leer el job, cae a 'default' (comportamiento antiguo)."""
    try:
        trigger = glue.get_trigger(Name=trigger_name)['Trigger']
        job = trigger['Actions'][0].get('JobName')
        return reglas_migracion.grupo_destino(job)
    except Exception:
        return 'default'


def asegurar_grupo(scheduler, grupo):
    """Crea el grupo de schedules si no existe (idempotente). Necesario porque
    ahora los schedules nacen en su grupo real: si el grupo no existe todavia,
    create_schedule falla con ResourceNotFoundException. 'default' siempre existe."""
    if not grupo or grupo == 'default':
        return
    from botocore.exceptions import ClientError
    try:
        scheduler.get_schedule_group(Name=grupo)
        return  # ya existe
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
    scheduler.create_schedule_group(Name=grupo)
    print(f"  ✅ grupo creado: {grupo}")


# =============================================================================
# CONSTRUIR PARÁMETROS DEL SCHEDULE (el mapeo)
# =============================================================================

def construir_params(trigger, estado):
    """Construye los parámetros de create_schedule. 'estado' = ENABLED/DISABLED.

    Devuelve una TUPLA: (params, revisar, motivo)
      - params : dict listo para scheduler.create_schedule(**params)
      - revisar: True si el cron es un caso delicado que NO se auto-convirtió
                 (dia-especifico con wrap, overflow). El llamador debe marcar la
                 fila 'revisar' y NO crear el schedule con un horario dudoso.
      - motivo : explicación corta (para la nota del control / log)

    APLICA LAS DECISIONES PREVIAS al momento de crear (leccion aprendida: que
    nazca correcto y no haya que reparar despues):
      * TIMEZONE  -> reglas_migracion.timezone_objetivo() (America/Santiago)
      * OFFSET    -> resta el desfase al cron con la politica A1
      * GRUPO     -> GroupName segun el job (regla de Bastian)
    """
    actions = trigger.get('Actions', [])
    if not actions:
        raise ValueError("El trigger no tiene Actions.")
    primer = actions[0]
    if 'JobName' not in primer:
        raise ValueError("El primer Action no tiene JobName (¿es un crawler?). No migrable con StartJobRun.")

    target_input = {'JobName': primer['JobName']}
    if primer.get('Arguments'):
        target_input['Arguments'] = primer['Arguments']
    if primer.get('Timeout') is not None:
        target_input['Timeout'] = primer['Timeout']
    if primer.get('SecurityConfiguration'):
        target_input['SecurityConfiguration'] = primer['SecurityConfiguration']
    if primer.get('NotificationProperty'):
        target_input['NotificationProperty'] = primer['NotificationProperty']

    # --- DECISIONES PREVIAS: timezone, offset de cron y grupo ---
    tz = reglas_migracion.timezone_objetivo()
    offset = reglas_migracion.offset_configurado()
    cron_original = trigger['Schedule']
    cron_final, revisar, motivo = reglas_migracion.convertir_cron_para_crear(cron_original, offset)
    grupo = reglas_migracion.grupo_destino(primer['JobName'])

    desc = (trigger.get('Description') or '').strip()
    # Si convertimos el cron, dejamos la marca [cron-local] (idempotencia con los
    # verificadores) y guardamos el cron UTC original para trazabilidad.
    prefijo = '[Migrado de Glue]'
    if offset and cron_final != cron_original:
        prefijo = f'[Migrado de Glue] [cron-local] (UTC orig: {cron_original})'
    descripcion_final = (prefijo + ' ' + desc).strip()[:512]

    params = {
        'Name': nombre_schedule_desde_trigger(trigger['Name']),
        'GroupName': grupo,
        'ScheduleExpression': cron_final,
        'ScheduleExpressionTimezone': tz,
        'FlexibleTimeWindow': {'Mode': 'OFF'},
        'State': estado,
        'Description': descripcion_final,
        'Target': {
            'Arn': 'arn:aws:scheduler:::aws-sdk:glue:startJobRun',
            'RoleArn': SCHEDULER_ROLE_ARN,
            'Input': json.dumps(target_input),
            'RetryPolicy': {
                'MaximumRetryAttempts': RETRY_ATTEMPTS,
                'MaximumEventAgeInSeconds': RETRY_MAX_AGE_SECONDS,
            },
        },
    }
    return params, revisar, motivo


# =============================================================================
# PASOS
# =============================================================================

def paso_crear(glue, scheduler, trigger_name):
    """PASO 1: crea el schedule DESACTIVADO."""
    print(f"\n=== PASO 1: CREAR (desactivado) ===")
    trigger = glue.get_trigger(Name=trigger_name)['Trigger']

    if trigger['Type'] != 'SCHEDULED':
        print(f"❌ El trigger es tipo {trigger['Type']}, no SCHEDULED. Aborto.")
        return

    if '<ACCOUNT_ID>' in SCHEDULER_ROLE_ARN or '<SCHEDULER_ROLE_NAME>' in SCHEDULER_ROLE_ARN:
        print("❌ SCHEDULER_ROLE_ARN es el placeholder. Define la variable de entorno "
              "SCHEDULER_ROLE_ARN o edítala en la sección CONFIGURACIÓN.")
        return

    # Siempre se crea DESACTIVADO en este flujo seguro
    params, revisar, motivo = construir_params(trigger, estado='DISABLED')
    nombre_schedule = params['Name']
    cron_original = trigger['Schedule']

    print(f"  Trigger origen: {trigger_name}")
    print(f"  Schedule nuevo: {nombre_schedule}  (se creará DISABLED)")
    print(f"  Grupo destino:  {params['GroupName']}")
    print(f"  Timezone:       {params['ScheduleExpressionTimezone']}")
    if params['ScheduleExpression'] != cron_original:
        print(f"  Cron:           {cron_original}  ->  {params['ScheduleExpression']}  ({motivo})")
    else:
        print(f"  Cron:           {params['ScheduleExpression']}  ({motivo})")
    print(f"  Job:            {json.loads(params['Target']['Input'])['JobName']}")

    # A1: si el cron es un caso delicado (dia-especifico con wrap, overflow),
    # NO lo creamos con un horario dudoso: avisamos y paramos para que un humano decida.
    if revisar:
        print(f"\n  ⚠️  REVISAR ANTES DE CREAR: {motivo}")
        print(f"     Este cron necesita criterio de negocio (el ajuste de hora movería el DÍA).")
        print(f"     No se creó el schedule. Opciones:")
        print(f"       - Corrige el offset/cron y decide la hora local correcta con negocio.")
        print(f"       - O crea igual con el cron tal cual si ya sabes que está bien.")
        return

    from botocore.exceptions import ClientError
    try:
        asegurar_grupo(scheduler, params['GroupName'])
        scheduler.create_schedule(**params)
        print(f"\n  ✅ Schedule '{nombre_schedule}' creado DESACTIVADO en grupo '{params['GroupName']}'.")
        print(f"     El Glue trigger sigue activo y funcionando (no se tocó).")
        print(f"     No hay doble ejecución porque el nuevo está apagado.")
        print(f"\n  👉 Siguiente: python migrar_seguro.py --trigger {trigger_name} --paso verificar")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConflictException':
            print(f"  ⚠️  Ya existía el schedule '{nombre_schedule}'. Revisa con --paso verificar.")
        else:
            print(f"  ❌ Error: {e}")


def paso_verificar(glue, scheduler, trigger_name):
    """PASO 2: compara el trigger viejo contra el schedule nuevo."""
    print(f"\n=== PASO 2: VERIFICAR ===")
    trigger = glue.get_trigger(Name=trigger_name)['Trigger']
    nombre_schedule = nombre_schedule_desde_trigger(trigger_name)
    grupo = reglas_migracion.grupo_destino(trigger['Actions'][0].get('JobName'))

    from botocore.exceptions import ClientError
    try:
        sched = scheduler.get_schedule(Name=nombre_schedule, GroupName=grupo)
    except ClientError:
        print(f"  ❌ El schedule '{nombre_schedule}' no existe en el grupo '{grupo}'. Corre --paso crear primero.")
        return

    t_job = trigger['Actions'][0].get('JobName')
    s_job = json.loads(sched['Target']['Input'])['JobName']

    # El cron del schedule pudo convertirse (offset aplicado al crear). Para
    # verificar no comparamos contra el cron ORIGINAL del trigger, sino contra
    # el cron ESPERADO tras aplicar la misma regla de conversion.
    offset = reglas_migracion.offset_configurado()
    cron_esperado, _, _ = reglas_migracion.convertir_cron_para_crear(trigger.get('Schedule', ''), offset)

    print(f"  {'Campo':<14} {'Glue Trigger':<28} {'EventBridge Schedule'}")
    print(f"  {'-'*14} {'-'*28} {'-'*28}")
    print(f"  {'cron origen':<14} {trigger.get('Schedule',''):<28}")
    print(f"  {'cron esperado':<14} {cron_esperado:<28} {sched['ScheduleExpression']}")
    print(f"  {'job':<14} {str(t_job):<28} {s_job}")
    print(f"  {'grupo':<14} {'':<28} {grupo}")
    print(f"  {'estado':<14} {trigger.get('State',''):<28} {sched['State']}")

    ok = (cron_esperado == sched['ScheduleExpression']) and (t_job == s_job)
    if ok:
        print(f"\n  ✅ Coinciden cron (convertido) y job. El schedule está listo (y DESACTIVADO).")
        print(f"\n  👉 Cuando estés listo para el switch:")
        print(f"     python migrar_seguro.py --trigger {trigger_name} --paso switch")
    else:
        print(f"\n  ❌ NO coinciden. Revisa antes de continuar. Puedes borrar el schedule")
        print(f"     y recrearlo, o revisar el mapeo/offset.")


def paso_switch(glue, scheduler, trigger_name):
    """PASO 3: apaga el Glue trigger y activa el schedule (seguidos)."""
    print(f"\n=== PASO 3: SWITCH (apagar viejo -> prender nuevo) ===")
    nombre_schedule = nombre_schedule_desde_trigger(trigger_name)
    grupo = grupo_de_trigger(glue, trigger_name)

    print(f"  ⚠️  Esto va a:")
    print(f"      1. DESACTIVAR el Glue trigger '{trigger_name}'")
    print(f"      2. ACTIVAR el schedule '{nombre_schedule}' (grupo '{grupo}')")
    print(f"  Orden seguro: primero apaga el viejo, luego prende el nuevo.")

    confirm = input("\n  ¿Confirmas el switch? Escribe 'si' para continuar: ").strip().lower()
    if confirm != 'si':
        print("  Cancelado. No se tocó nada.")
        return

    # 1. Apagar el Glue trigger
    print(f"\n  → Desactivando Glue trigger '{trigger_name}'...")
    glue.stop_trigger(Name=trigger_name)
    print(f"    ✅ Glue trigger desactivado.")

    # 2. Activar el schedule (update a ENABLED). Hay que reenviar todos los params.
    print(f"  → Activando schedule '{nombre_schedule}'...")
    sched = scheduler.get_schedule(Name=nombre_schedule, GroupName=grupo)
    scheduler.update_schedule(
        Name=nombre_schedule,
        GroupName=grupo,
        ScheduleExpression=sched['ScheduleExpression'],
        ScheduleExpressionTimezone=sched.get('ScheduleExpressionTimezone', TIMEZONE),
        FlexibleTimeWindow=sched['FlexibleTimeWindow'],
        Target=sched['Target'],
        State='ENABLED',
        Description=sched.get('Description', ''),
    )
    print(f"    ✅ Schedule ACTIVADO.")
    print(f"\n  🎉 Switch completo. Ahora el job lo dispara EventBridge Scheduler.")
    print(f"\n  👉 Monitorea la próxima ejecución. Revisa estado con:")
    print(f"     python migrar_seguro.py --trigger {trigger_name} --paso estado")
    print(f"     NO elimines el trigger viejo hasta confirmar varios días OK.")


def paso_estado(glue, scheduler, trigger_name):
    """PASO 4: muestra el estado actual de ambos (para monitorear)."""
    print(f"\n=== PASO 4: ESTADO ACTUAL ===")
    nombre_schedule = nombre_schedule_desde_trigger(trigger_name)
    grupo = grupo_de_trigger(glue, trigger_name)
    from botocore.exceptions import ClientError

    try:
        trigger = glue.get_trigger(Name=trigger_name)['Trigger']
        print(f"  Glue trigger '{trigger_name}': {trigger.get('State')}")
    except ClientError:
        print(f"  Glue trigger '{trigger_name}': (no existe / eliminado)")

    try:
        sched = scheduler.get_schedule(Name=nombre_schedule, GroupName=grupo)
        print(f"  Schedule '{nombre_schedule}' (grupo {grupo}): {sched['State']}")
    except ClientError:
        print(f"  Schedule '{nombre_schedule}' (grupo {grupo}): (no existe)")

    print(f"\n  Estado ideal tras el switch: trigger DEACTIVATED + schedule ENABLED")


def paso_rollback(glue, scheduler, trigger_name):
    """ROLLBACK: deshace el switch. Reactiva el Glue trigger y desactiva el schedule.
    Úsalo si algo sale mal después del switch. Te devuelve al estado original."""
    print(f"\n=== ROLLBACK (deshacer switch) ===")
    nombre_schedule = nombre_schedule_desde_trigger(trigger_name)
    grupo = grupo_de_trigger(glue, trigger_name)
    print(f"  Esto va a:")
    print(f"    1. DESACTIVAR el schedule nuevo '{nombre_schedule}' (grupo '{grupo}')")
    print(f"    2. REACTIVAR el Glue trigger original '{trigger_name}'")
    print(f"  → Vuelves al estado ANTES de la migración.")

    confirm = input("\n  ¿Confirmas el rollback? Escribe 'si' para continuar: ").strip().lower()
    if confirm != 'si':
        print("  Cancelado. No se tocó nada.")
        return

    from botocore.exceptions import ClientError

    # 1. Desactivar el schedule nuevo
    try:
        sched = scheduler.get_schedule(Name=nombre_schedule, GroupName=grupo)
        scheduler.update_schedule(
            Name=nombre_schedule,
            GroupName=grupo,
            ScheduleExpression=sched['ScheduleExpression'],
            ScheduleExpressionTimezone=sched.get('ScheduleExpressionTimezone', TIMEZONE),
            FlexibleTimeWindow=sched['FlexibleTimeWindow'],
            Target=sched['Target'],
            State='DISABLED',
            Description=sched.get('Description', ''),
        )
        print(f"\n  → Schedule '{nombre_schedule}' DESACTIVADO.")
    except ClientError as e:
        print(f"  ⚠️  No se pudo desactivar el schedule: {e}")

    # 2. Reactivar el Glue trigger original
    try:
        glue.start_trigger(Name=trigger_name)
        print(f"  → Glue trigger '{trigger_name}' REACTIVADO.")
        print(f"\n  ✅ Rollback completo. Estás de vuelta en el estado original.")
    except ClientError as e:
        print(f"  ❌ No se pudo reactivar el trigger: {e}")
        print(f"     ¡ATENCIÓN! Reactívalo manualmente en la consola de Glue.")


def paso_verificar_ejecucion(glue, scheduler, trigger_name):
    """Verifica si el JOB realmente se ejecutó (revisa los últimos job runs).
    Úsalo DESPUÉS del switch, tras la hora programada, para confirmar que
    EventBridge efectivamente disparó el job (detecta el fallo silencioso del IAM Role)."""
    print(f"\n=== VERIFICAR EJECUCIÓN DEL JOB ===")
    # Obtener el nombre del job desde el trigger (o del schedule si el trigger ya no existe)
    from botocore.exceptions import ClientError
    job_name = None
    try:
        trigger = glue.get_trigger(Name=trigger_name)['Trigger']
        job_name = trigger['Actions'][0].get('JobName')
    except ClientError:
        nombre_schedule = nombre_schedule_desde_trigger(trigger_name)
        try:
            sched = scheduler.get_schedule(Name=nombre_schedule)
            job_name = json.loads(sched['Target']['Input'])['JobName']
        except ClientError:
            print("  No pude determinar el job. Aborto.")
            return

    print(f"  Revisando últimas ejecuciones de: {job_name}\n")
    try:
        runs = glue.get_job_runs(JobName=job_name, MaxResults=5)['JobRuns']
    except ClientError as e:
        print(f"  ❌ No se pudieron leer las ejecuciones: {e}")
        return

    if not runs:
        print("  ⚠️  No hay ejecuciones registradas todavía.")
        print("     Si ya pasó la hora programada y no hay runs -> posible fallo del IAM Role.")
        return

    for r in runs:
        started = r.get('StartedOn')
        estado = r.get('JobRunState')
        print(f"    {started}  ->  {estado}")
    print(f"\n  Si ves una ejecución reciente tras el switch con estado SUCCEEDED/RUNNING,")
    print(f"  la migración funcionó. Si no aparece nada nuevo -> revisa el IAM Role / DLQ.")


def paso_limpiar(glue, scheduler, trigger_name):
    """PASO 5: elimina el Glue trigger viejo (SOLO tras monitorear días)."""
    print(f"\n=== PASO 5: LIMPIAR (eliminar Glue trigger viejo) ===")
    print(f"  ⚠️  Esto ELIMINA permanentemente el Glue trigger '{trigger_name}'.")
    print(f"  Solo hazlo si el schedule lleva varios días ejecutando bien.")

    confirm = input("\n  ¿Seguro? Escribe 'ELIMINAR' para confirmar: ").strip()
    if confirm != 'ELIMINAR':
        print("  Cancelado. No se eliminó nada.")
        return

    glue.delete_trigger(Name=trigger_name)
    print(f"  ✅ Glue trigger '{trigger_name}' eliminado.")
    print(f"     La migración de este trigger está COMPLETA.")


PASOS = {
    'crear': paso_crear,
    'verificar': paso_verificar,
    'switch': paso_switch,
    'estado': paso_estado,
    'verificar-ejecucion': paso_verificar_ejecucion,
    'rollback': paso_rollback,
    'limpiar': paso_limpiar,
}


# =============================================================================
# MAIN
# =============================================================================

def main():
    args = parse_args()

    if args.demo:
        import boto3
        from moto import mock_aws

        # En demo mantenemos estado entre pasos usando un solo mock por corrida;
        # como cada corrida es independiente, el demo ejecuta el flujo completo.
        @mock_aws
        def _demo():
            global SCHEDULER_ROLE_ARN
            SCHEDULER_ROLE_ARN = 'arn:aws:iam::111111111111:role/DemoRole'
            glue = boto3.client('glue', region_name=args.region)
            scheduler = boto3.client('scheduler', region_name=args.region)

            glue.create_job(Name='sdlf-bigdata-demo-glue-job', Role='arn:aws:iam::111111111111:role/R',
                            Command={'Name': 'glueetl', 'ScriptLocation': 's3://x/demo.py', 'PythonVersion': '3'})
            glue.create_trigger(Name='sdlf-bigdata-demo-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 9 * * ? *)',
                                Description='Trigger de prueba no critico',
                                Actions=[{'JobName': 'sdlf-bigdata-demo-glue-job'}])

            print("### DEMO: ejecutando el flujo completo de corrido ###")
            paso_crear(glue, scheduler, args.trigger)
            paso_verificar(glue, scheduler, args.trigger)
            print("\n(DEMO) Simulando el switch sin pedir confirmación...")
            grupo_demo = grupo_de_trigger(glue, args.trigger)
            glue.stop_trigger(Name=args.trigger)
            s = scheduler.get_schedule(Name=nombre_schedule_desde_trigger(args.trigger),
                                       GroupName=grupo_demo)
            scheduler.update_schedule(
                Name=s['Name'], GroupName=grupo_demo,
                ScheduleExpression=s['ScheduleExpression'],
                FlexibleTimeWindow=s['FlexibleTimeWindow'], Target=s['Target'],
                State='ENABLED', Description=s.get('Description', ''))
            print("  ✅ (DEMO) switch simulado")
            paso_estado(glue, scheduler, args.trigger)

        _demo()
        return

    # --- Modo real ---
    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError
    try:
        glue = boto3.client('glue', region_name=args.region)
        scheduler = boto3.client('scheduler', region_name=args.region)

        # Paso especial: solo regenerar el reporte (sin migrar nada).
        if args.paso == 'reporte':
            regenerar_reporte_seguimiento(scheduler, glue)
            return

        # Los demás pasos necesitan --trigger
        if not args.trigger:
            print("❌ Este paso requiere --trigger NOMBRE-DEL-TRIGGER")
            return

        PASOS[args.paso](glue, scheduler, args.trigger)

        # Automatización: tras un paso que CAMBIA el estado, regenerar el
        # reporte de seguimiento (a menos que se pida --sin-reporte).
        pasos_que_cambian_estado = {'crear', 'switch', 'rollback', 'limpiar'}
        if args.paso in pasos_que_cambian_estado and not args.sin_reporte:
            regenerar_reporte_seguimiento(scheduler, glue)

    except NoCredentialsError:
        print("❌ No hay credenciales AWS. Configúralas o usa --demo.")
    except ClientError as e:
        print(f"❌ Error de AWS: {e}")


def regenerar_reporte_seguimiento(scheduler, glue):
    """Regenera el reporte HTML de seguimiento reutilizando reporte_seguimiento.py.
    Si el módulo no está disponible, avisa pero no rompe la migración."""
    print("\n" + "-" * 50)
    print("Actualizando reporte de seguimiento...")
    try:
        import reporte_seguimiento
    except ImportError:
        print("  (aviso) no encontré reporte_seguimiento.py en esta carpeta; se omite el reporte.")
        return
    try:
        datos = reporte_seguimiento.construir_datos(scheduler, glue)
        html_out = reporte_seguimiento.generar_html(datos)
        with open('reporte_seguimiento.html', 'w', encoding='utf-8') as f:
            f.write(html_out)
        print(f"  ✅ reporte_seguimiento.html actualizado ({len(datos)} schedules migrados)")
    except Exception as e:
        print(f"  (aviso) no se pudo regenerar el reporte: {type(e).__name__}: {e}")
        print("  La migración SÍ se completó; solo falló el reporte.")


def parse_args():
    p = argparse.ArgumentParser(description='Migración segura de 1 trigger, paso a paso')
    p.add_argument('--trigger', help='Nombre exacto del Glue trigger (no requerido para --paso reporte)')
    p.add_argument('--paso', choices=list(PASOS.keys()) + ['reporte'],
                   help="Paso a ejecutar. 'reporte' = solo actualizar el HTML sin migrar")
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--demo', action='store_true', help='Practicar el flujo con moto')
    p.add_argument('--sin-reporte', action='store_true',
                   help='No regenerar el reporte de seguimiento tras la migración')
    return p.parse_args()


if __name__ == '__main__':
    main()
