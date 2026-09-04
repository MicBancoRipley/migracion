"""
Migración puntual del caso E (choque lnegro_car) con NOMBRE CUSTOM.

Bastian pidió que el schedule del trigger sp-tnda-forma-pago-en-tda se llame
'sp_ctbl_lnegro_car' (no el nombre derivado del trigger). El lote no genera
nombres custom, así que este script lo hace a mano, reusando construir_params
de migrar_seguro para que el mapeo (job, cron, args) sea idéntico al resto.

Flujo seguro (igual que el lote): crea DISABLED -> verifica -> switch.

Uso:
    python migrar_E_lnegro.py --paso crear    --region us-east-1
    python migrar_E_lnegro.py --paso verificar --region us-east-1
    python migrar_E_lnegro.py --paso switch    --region us-east-1
"""
import argparse
import json
import migrar_seguro

TRIGGER = 'sdlf-bigdata-sp-tnda-forma-pago-en-tda-glue-trigger'
NOMBRE_SCHEDULE = 'sp_ctbl_lnegro_car'   # nombre custom pedido por Bastian


def _params(glue, estado):
    trigger = glue.get_trigger(Name=TRIGGER)['Trigger']
    p = migrar_seguro.construir_params(trigger, estado=estado)
    p['Name'] = NOMBRE_SCHEDULE          # sobreescribir con el nombre custom
    return p


def crear(glue, scheduler):
    from botocore.exceptions import ClientError
    p = _params(glue, estado='DISABLED')
    print(f"Creando schedule '{NOMBRE_SCHEDULE}' DISABLED")
    print(f"  cron={p['ScheduleExpression']}  job={json.loads(p['Target']['Input'])['JobName']}")
    try:
        scheduler.create_schedule(**p)
        print("  OK creado DISABLED")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConflictException':
            print("  ya existía; sigue con verificar")
        else:
            raise


def verificar(glue, scheduler):
    trigger = glue.get_trigger(Name=TRIGGER)['Trigger']
    sched = scheduler.get_schedule(Name=NOMBRE_SCHEDULE)
    t_job = trigger['Actions'][0].get('JobName')
    s_job = json.loads(sched['Target']['Input'])['JobName']
    ok = (trigger.get('Schedule') == sched['ScheduleExpression']) and (t_job == s_job)
    print(f"  cron trigger={trigger.get('Schedule')}  schedule={sched['ScheduleExpression']}")
    print(f"  job  trigger={t_job}  schedule={s_job}")
    print("  ✅ coinciden" if ok else "  ❌ NO coinciden - revisar")


def switch(glue, scheduler):
    estado_glue = glue.get_trigger(Name=TRIGGER)['Trigger'].get('State')
    print(f"  estado trigger: {estado_glue}")
    if estado_glue == 'ACTIVATED':
        confirm = input("  Escribe 'MIGRAR' para apagar trigger y activar schedule: ").strip()
        if confirm != 'MIGRAR':
            print("  cancelado"); return
        glue.stop_trigger(Name=TRIGGER)
        print("  trigger DESACTIVADO")
        estado_sched = 'ENABLED'
    else:
        estado_sched = 'DISABLED'
    sched = scheduler.get_schedule(Name=NOMBRE_SCHEDULE)
    scheduler.update_schedule(
        Name=NOMBRE_SCHEDULE, ScheduleExpression=sched['ScheduleExpression'],
        ScheduleExpressionTimezone=sched.get('ScheduleExpressionTimezone', 'UTC'),
        FlexibleTimeWindow=sched['FlexibleTimeWindow'], Target=sched['Target'],
        State=estado_sched, Description=sched.get('Description', ''))
    print(f"  schedule -> {estado_sched}. Listo.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--paso', choices=['crear', 'verificar', 'switch'], required=True)
    ap.add_argument('--region', default='us-east-1')
    args = ap.parse_args()
    import boto3
    glue = boto3.client('glue', region_name=args.region)
    scheduler = boto3.client('scheduler', region_name=args.region)
    {'crear': crear, 'verificar': verificar, 'switch': switch}[args.paso](glue, scheduler)


if __name__ == '__main__':
    main()
