"""
=============================================================================
REPORTE HTML DE SEGUIMIENTO DE MIGRACIÓN
=============================================================================

Genera un HTML para hacer seguimiento de los triggers YA migrados a
EventBridge Scheduler. Por cada schedule migrado muestra:

  - Nombre del schedule (y trigger de origen)
  - Hora de creación/migración del schedule
  - Estado del schedule (ENABLED / DISABLED)
  - Parámetros que se pasaron (job, cron, args, input completo)
  - ¿El job YA se disparó tras la migración?  ->  DISPARADO / PENDIENTE
    (comprueba los job runs de Glue posteriores a la creación del schedule)

¿Cómo identifica los "ya migrados"?
  Los schedules creados por nuestro flujo terminan en '-schedule' y su
  Description empieza con '[Migrado de Glue]'. Ese es el criterio por defecto.

Es SOLO LECTURA. Permisos: scheduler:ListSchedules, scheduler:GetSchedule,
                           glue:GetJobRuns

USO:
    python reporte_seguimiento.py --region us-east-1
    python reporte_seguimiento.py --demo
    # Genera: reporte_seguimiento.html
=============================================================================
"""

import html
import json
import argparse
import webbrowser
from datetime import datetime, timezone


PREFIJO_SCHEDULE = 'sdlf-bigdata'      # solo seguimos schedules de este proyecto
MARCA_MIGRADO = '[Migrado de Glue]'   # así identificamos los que migramos nosotros


# =============================================================================
# LECTURA (solo lectura)
# =============================================================================

def listar_schedules_migrados(scheduler):
    """Lista los schedules que creamos nosotros (por marca en la Description).

    Robusto: primero recolecta TODOS los nombres (con paginación completa),
    luego pide el detalle de cada uno tolerando errores individuales para que
    un schedule problemático no rompa el reporte completo.
    """
    from botocore.exceptions import ClientError

    # 1. Recolectar todos los nombres primero (paginación completa)
    nombres = []
    next_token = None
    while True:
        kwargs = {'MaxResults': 100}
        if next_token:
            kwargs['NextToken'] = next_token
        resp = scheduler.list_schedules(**kwargs)
        for s in resp.get('Schedules', []):
            nombres.append(s['Name'])
        next_token = resp.get('NextToken')
        if not next_token:
            break

    # 2. Pre-filtrar por nombre (más rápido y seguro): solo los que terminan en
    #    '-schedule' Y empiezan con el prefijo del proyecto (sdlf-bigdata).
    #    Así ignoramos schedules de otros equipos/proyectos.
    candidatos = [
        n for n in nombres
        if n.endswith('-schedule') and n.startswith(PREFIJO_SCHEDULE)
    ]

    # 3. Pedir el detalle de cada candidato, tolerando errores individuales
    migrados = []
    for nombre in candidatos:
        try:
            detalle = scheduler.get_schedule(Name=nombre)
        except ClientError as e:
            print(f"  (aviso) no se pudo leer '{nombre}': {e.response['Error']['Code']}")
            continue
        desc = detalle.get('Description', '') or ''
        if MARCA_MIGRADO in desc or nombre.endswith('-schedule'):
            migrados.append(detalle)
    return migrados


def _sql_file_key_de_run(run):
    """Extrae el --sql_file_key de los Arguments de un job run (o None)."""
    args = run.get('Arguments') or {}
    return args.get('--sql_file_key')


def _lo_disparo_eventbridge(run):
    """True si la corrida NO fue lanzada por un Glue trigger.

    Cuando un GLUE TRIGGER dispara el job, la corrida trae TriggerName con el
    nombre del trigger (lo confirma la consola: columna 'Trigger name').
    Cuando EVENTBRIDGE SCHEDULER dispara (via StartJobRun directo), NO hay
    trigger asociado -> TriggerName viene vacío/ausente. Esa es la firma de
    que la MIGRACIÓN tomó el control del disparo.
    """
    tn = (run.get('TriggerName') or '').strip()
    return tn == ''


def _estado_trigger_viejo(glue, trigger_name):
    """Devuelve el State actual del Glue trigger viejo, o None si no existe.

    Sirve para distinguir un AMBIGUO real (trigger sigue ACTIVATED = doble
    disparo) de una falsa alarma (trigger ya DEACTIVATED/eliminado = la corrida
    que vimos fue el 'último suspiro' antes de apagarlo)."""
    from botocore.exceptions import ClientError
    if not trigger_name:
        return None
    try:
        return glue.get_trigger(Name=trigger_name)['Trigger'].get('State')
    except ClientError:
        return 'ELIMINADO'  # ya no existe: alguien lo borró tras migrar


def comprobar_disparo(glue, job_name, activado_dt, sql_file_key=None, trigger_name=None,
                      job_compartido=True):
    """
    Comprueba si ESTE schedule específico disparó el job DESPUÉS de activarse.

    IMPORTANTE (lección aprendida en la migración real):
      El momento de referencia NO es cuándo se CREÓ el schedule (CreationDate),
      porque en el flujo seguro lo creamos DESACTIVADO primero: en ese momento
      todavía NO puede disparar nada. El momento correcto es cuándo pasó a
      ENABLED (el 'switch'), que AWS registra en LastModificationDate.

    PROBLEMA CRÍTICO (falso positivo): CIENTOS de schedules apuntan al MISMO
      job (sdlf-bigdata-redshift-segmentation-schedule-glue-job). get_job_runs
      devuelve TODAS las corridas del job, sin importar quién las lanzó. Si solo
      filtráramos por fecha, CADA schedule saldría 'DISPARADO' porque otros
      cientos disparan el job todo el tiempo -> falso positivo.

    SOLUCIÓN: cada schedule pasa un --sql_file_key ÚNICO al job compartido.
      Para saber si ESTE schedule disparó, buscamos una corrida que sea
      (1) posterior a su activación Y (2) tenga SU MISMO --sql_file_key.

    Devuelve dict con estado: DISPARADO / PENDIENTE / SIN_JOB / ERROR / AMBIGUO.
    """
    from botocore.exceptions import ClientError
    if not job_name:
        return {'estado': 'SIN_JOB', 'detalle': 'No hay job asociado'}
    try:
        runs = glue.get_job_runs(JobName=job_name, MaxResults=200)['JobRuns']
    except ClientError as e:
        return {'estado': 'ERROR', 'detalle': f'{type(e).__name__}'}

    if not runs:
        return {'estado': 'PENDIENTE', 'detalle': 'Sin ejecuciones registradas todavía'}

    # Normalizar tz: si por lo que sea llega naive, lo tratamos como UTC para
    # poder comparar sin lanzar TypeError.
    def _aware(dt):
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    activado_dt = _aware(activado_dt)

    # CASO A: el job NO es compartido (este schedule es su único dueño, p.ej.
    # los de monitoreo: cost-monitor, iam-changes, etc.). No necesita
    # --sql_file_key para desambiguar: basta ver si SU job corrió tras el switch.
    if not job_compartido:
        eb = [r for r in runs
              if _lo_disparo_eventbridge(r)
              and _aware(r.get('StartedOn')) and activado_dt
              and _aware(r.get('StartedOn')) >= activado_dt]
        if eb:
            p = max(eb, key=lambda r: _aware(r.get('StartedOn')))
            return {'estado': 'DISPARADO',
                    'detalle': f"{p.get('JobRunState')} @ {_fmt_utc(_aware(p.get('StartedOn')))} "
                               f"(job exclusivo, EventBridge)",
                    'run_state': p.get('JobRunState')}
        # ¿corrió por el trigger viejo tras el switch? -> chequear si sigue activo
        por_trig = [r for r in runs
                    if not _lo_disparo_eventbridge(r)
                    and _aware(r.get('StartedOn')) and activado_dt
                    and _aware(r.get('StartedOn')) >= activado_dt]
        if por_trig:
            u = max(por_trig, key=lambda r: _aware(r.get('StartedOn')))
            est = _estado_trigger_viejo(glue, trigger_name or (u.get('TriggerName') or '').strip())
            if est == 'ACTIVATED':
                return {'estado': 'AMBIGUO',
                        'detalle': f"⚠️ DOBLE DISPARO: trigger viejo ACTIVATED @ "
                                   f"{_fmt_utc(_aware(u.get('StartedOn')))}. Apágalo."}
            return {'estado': 'DISPARADO',
                    'detalle': f"OK. Último disparo por trigger viejo @ "
                               f"{_fmt_utc(_aware(u.get('StartedOn')))}; quedó {est}.",
                    'run_state': u.get('JobRunState')}
        return {'estado': 'PENDIENTE',
                'detalle': 'Job exclusivo sin corridas tras el switch todavía.'}

    # CASO B: el job ES compartido. Sin --sql_file_key NO podemos discriminar
    # quién disparó -> lo decimos claramente (no mentimos).
    if not sql_file_key:
        return {
            'estado': 'AMBIGUO',
            'detalle': ('No se puede confirmar: el job es compartido por cientos de '
                        'schedules y este no tiene --sql_file_key para distinguirlo.'),
        }

    # Corridas de ESTE schedule, disparadas por EVENTBRIDGE (no por trigger):
    #   (1) mismo --sql_file_key  (2) posteriores al switch  (3) TriggerName vacío
    mias_por_sql = [r for r in runs if _sql_file_key_de_run(r) == sql_file_key]
    mias_eb = [r for r in mias_por_sql
               if _lo_disparo_eventbridge(r)
               and _aware(r.get('StartedOn')) and activado_dt
               and _aware(r.get('StartedOn')) >= activado_dt]

    if mias_eb:
        p = max(mias_eb, key=lambda r: _aware(r.get('StartedOn')))
        return {
            'estado': 'DISPARADO',
            'detalle': (f"{p.get('JobRunState')} @ {_fmt_utc(_aware(p.get('StartedOn')))} "
                        f"(EventBridge, sin trigger — confirmado)"),
            'run_state': p.get('JobRunState'),
        }

    # ¿Hay corridas de MI sql posteriores al switch pero que aún trae el TRIGGER
    # viejo? -> el trigger no se ha apagado/eliminado: sigue disparando él.
    mias_post = [r for r in mias_por_sql
                 if _aware(r.get('StartedOn')) and activado_dt
                 and _aware(r.get('StartedOn')) >= activado_dt]
    aun_por_trigger = [r for r in mias_post if not _lo_disparo_eventbridge(r)]
    if aun_por_trigger:
        u = max(aun_por_trigger, key=lambda r: _aware(r.get('StartedOn')))
        nombre_trig = (u.get('TriggerName') or '').strip()
        # ¿El trigger viejo sigue vivo? Eso decide si es alarma real o falsa.
        estado_trig = _estado_trigger_viejo(glue, trigger_name or nombre_trig)
        if estado_trig == 'ACTIVATED':
            # 🔴 DOBLE DISPARO real: trigger viejo activo + schedule nuevo activo.
            return {
                'estado': 'AMBIGUO',
                'detalle': (f"⚠️ DOBLE DISPARO: el trigger viejo '{nombre_trig}' sigue ACTIVATED "
                            f"y lanzó el job @ {_fmt_utc(_aware(u.get('StartedOn')))}. "
                            f"Apágalo: el schedule nuevo ya está activo."),
            }
        # Trigger ya DEACTIVATED/ELIMINADO -> la corrida vista fue el último
        # disparo del trigger justo antes de apagarlo. NO hay doble disparo.
        return {
            'estado': 'DISPARADO',
            'detalle': (f"OK. Última corrida por el trigger viejo @ "
                        f"{_fmt_utc(_aware(u.get('StartedOn')))} fue su último disparo; "
                        f"el trigger quedó {estado_trig}. EventBridge ya tiene el control."),
            'run_state': u.get('JobRunState'),
        }

    # No hay corrida propia posterior al switch todavía.
    if mias_por_sql:
        ult = max(mias_por_sql, key=lambda r: _aware(r.get('StartedOn')))
        quien = (ult.get('TriggerName') or '').strip() or 'EventBridge'
        detalle = (f"Aún sin ejecución PROPIA tras activarse. Último run de este "
                   f"sql_file_key: {ult.get('JobRunState')} @ {_fmt_utc(_aware(ult.get('StartedOn')))} "
                   f"(lo lanzó: {quien})")
    else:
        detalle = ("Sin ninguna corrida con este --sql_file_key en las últimas 200 "
                   "(puede que aún no toque su cron, o mirar más historial).")
    if activado_dt:
        detalle += f" (schedule activo desde {activado_dt.strftime('%Y-%m-%d %H:%M UTC')})"
    return {'estado': 'PENDIENTE', 'detalle': detalle}


# =============================================================================
# CONSTRUIR DATOS
# =============================================================================

def _fmt_utc(dt):
    """Formatea un datetime SIEMPRE en UTC real (convirtiendo si trae otra tz).

    Esto evita el error de mostrar una hora local con la etiqueta 'UTC'.
    boto3 normalmente entrega datetimes tz-aware; los convertimos a UTC de
    forma explícita antes de mostrar. Si llega naive, lo asumimos ya en UTC.
    """
    if not dt:
        return '—'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def construir_datos(scheduler, glue):
    schedules = listar_schedules_migrados(scheduler)

    # Contar cuántos schedules apuntan a cada job. Un job usado por >1 schedule
    # es "compartido" (p.ej. redshift-segmentation) y necesita --sql_file_key
    # para desambiguar. Un job usado por 1 solo schedule es exclusivo (monitoreo).
    from collections import Counter
    conteo_job = Counter()
    for s in schedules:
        try:
            _inp = json.loads(s.get('Target', {}).get('Input', '{}'))
        except Exception:
            _inp = {}
        _job = _inp.get('JobName')
        if _job:
            conteo_job[_job] += 1

    datos = []
    for s in schedules:
        target = s.get('Target', {})
        try:
            inp = json.loads(target.get('Input', '{}'))
        except Exception:
            inp = {}
        job_name = inp.get('JobName')

        creado = s.get('CreationDate')            # cuándo se creó (DESACTIVADO)
        modificado = s.get('LastModificationDate')  # cuándo se activó (switch)

        # El momento de referencia para "¿ya disparó?" es cuándo quedó ACTIVO.
        # Si está ENABLED, usamos LastModificationDate (el switch). Como respaldo,
        # si no viene, caemos a CreationDate.
        activado_dt = modificado or creado
        # El --sql_file_key ÚNICO de este schedule (para distinguir su corrida
        # dentro del job compartido por cientos de schedules).
        sql_file_key = (inp.get('Arguments') or {}).get('--sql_file_key')
        trigger_viejo = s.get('Name', '').replace('-schedule', '-trigger')
        job_compartido = conteo_job.get(job_name, 0) > 1
        disparo = comprobar_disparo(glue, job_name, activado_dt, sql_file_key,
                                    trigger_viejo, job_compartido)

        # --- DIAGNÓSTICO por consola (todo en UTC real, para no adivinar) ---
        def _u(dt):
            if not dt:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        print(f"\n  [diag] {s.get('Name')}")
        print(f"         estado schedule   : {s.get('State')}")
        print(f"         creado       (UTC): {_u(creado)}")
        print(f"         switch/activado(UTC): {_u(modificado)}")
        print(f"         referencia usada (UTC): {_u(activado_dt)}")
        try:
            _runs = glue.get_job_runs(JobName=job_name, MaxResults=3)['JobRuns'] if job_name else []
            _ref = _u(activado_dt)
            for _r in _runs:
                _st = _u(_r.get('StartedOn'))
                _mayor = (_st >= _ref) if (_st and _ref) else '?'
                print(f"         run (UTC): {_st}  {_r.get('JobRunState')}  ¿>=ref? {_mayor}")
        except Exception as _e:
            print(f"         (no se pudieron listar runs para diag: {_e})")
        print(f"         => {disparo['estado']}")

        datos.append({
            'schedule': s.get('Name'),
            'trigger_origen': s.get('Name', '').replace('-schedule', '-trigger'),
            'estado': s.get('State'),
            'creado': _fmt_utc(creado),
            'activado': _fmt_utc(modificado),
            'cron': s.get('ScheduleExpression', '—'),
            'timezone': s.get('ScheduleExpressionTimezone', 'UTC'),
            'job': job_name or '—',
            'input': json.dumps(inp, ensure_ascii=False),
            'role_arn': target.get('RoleArn', '—'),
            'disparo_estado': disparo['estado'],
            'disparo_detalle': disparo['detalle'],
        })
    # Ordenar: primero los PENDIENTE, luego DISPARADO
    orden = {'PENDIENTE': 0, 'AMBIGUO': 1, 'DISPARADO': 2, 'SIN_JOB': 3, 'SIN_DATOS': 4, 'ERROR': 5}
    datos.sort(key=lambda d: orden.get(d['disparo_estado'], 9))
    return datos


# =============================================================================
# HTML
# =============================================================================

def generar_html(datos):
    total = len(datos)
    disparados = sum(1 for d in datos if d['disparo_estado'] == 'DISPARADO')
    pendientes = sum(1 for d in datos if d['disparo_estado'] == 'PENDIENTE')
    enabled = sum(1 for d in datos if d['estado'] == 'ENABLED')
    datos_json = json.dumps(datos, ensure_ascii=False)

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seguimiento de Migración - Glue &rarr; EventBridge Scheduler</title>
<style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f1f5f9; color:#0f172a; margin:0; padding:24px; }
    h1 { font-size:22px; margin:0 0 4px; }
    .sub { color:#64748b; margin-bottom:20px; font-size:14px; }
    .kpis { display:flex; gap:12px; margin-bottom:18px; flex-wrap:wrap; }
    .kpi { background:#fff; border-radius:10px; padding:14px 20px; box-shadow:0 1px 3px rgba(0,0,0,.08); flex:1; min-width:130px; cursor:pointer; border:2px solid transparent; }
    .kpi:hover { border-color:#cbd5e1; }
    .kpi.active { border-color:#0ea5e9; }
    .kpi .n { font-size:28px; font-weight:700; }
    .kpi .l { color:#64748b; font-size:12px; }
    .controls { display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; align-items:center; position:sticky; top:0; background:#f1f5f9; padding:8px 0; z-index:10; }
    #buscador { flex:1; min-width:220px; padding:10px 14px; border:1px solid #cbd5e1; border-radius:8px; font-size:14px; }
    .count { color:#64748b; font-size:13px; white-space:nowrap; }
    table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); }
    thead th { text-align:left; font-size:11px; color:#64748b; padding:10px 14px; background:#f8fafc; text-transform:uppercase; border-bottom:1px solid #e2e8f0; }
    tbody tr.row { border-bottom:1px solid #f1f5f9; cursor:pointer; }
    tbody tr.row:hover { background:#f8fafc; }
    td { padding:10px 14px; font-size:13px; vertical-align:top; }
    .mono { font-family:ui-monospace, monospace; font-size:12px; word-break:break-all; }
    .pill { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; }
    .p-DISPARADO { background:#dcfce7; color:#16a34a; }
    .p-PENDIENTE { background:#fef9c3; color:#a16207; }
    .p-AMBIGUO { background:#ffedd5; color:#c2410c; }
    .p-SIN_JOB, .p-SIN_DATOS { background:#f1f5f9; color:#64748b; }
    .p-ERROR { background:#fee2e2; color:#dc2626; }
    .est-ENABLED { color:#16a34a; font-weight:600; }
    .est-DISABLED { color:#94a3b8; font-weight:600; }
    .detalle { background:#f8fafc; }
    .detalle td { padding:0; }
    .detalle-inner { padding:14px 24px; }
    .ptable { width:100%; border-collapse:collapse; background:transparent; box-shadow:none; }
    .ptable td { padding:6px 12px; border-bottom:1px solid #eef2f7; }
    .pname { font-weight:600; color:#475569; width:170px; font-size:12px; }
    .pval { font-family:ui-monospace, monospace; font-size:12px; word-break:break-all; }
    .foot { color:#94a3b8; font-size:12px; margin-top:20px; text-align:center; }
    .vacio { text-align:center; color:#94a3b8; padding:40px; }
</style>
</head>
<body>
    <h1>Seguimiento de Migración: Glue &rarr; EventBridge Scheduler</h1>
    <div class="sub">Generado el __FECHA__ &middot; Solo lectura &middot; Muestra los triggers YA migrados y si su job ya se disparó</div>

    <div class="kpis">
        <div class="kpi active" data-filtro="todos"><div class="n">__TOTAL__</div><div class="l">Migrados</div></div>
        <div class="kpi" data-filtro="ENABLED"><div class="n" style="color:#16a34a">__ENABLED__</div><div class="l">Schedules activos</div></div>
        <div class="kpi" data-filtro="DISPARADO"><div class="n" style="color:#16a34a">__DISPARADOS__</div><div class="l">&#10003; Job ya disparado</div></div>
        <div class="kpi" data-filtro="PENDIENTE"><div class="n" style="color:#a16207">__PENDIENTES__</div><div class="l">&#8987; Pendiente de disparo</div></div>
    </div>

    <div class="controls">
        <input id="buscador" type="text" placeholder="Buscar por schedule, job, cron...">
        <span class="count" id="contador"></span>
    </div>

    <table>
        <thead><tr>
            <th>Disparo</th><th>Schedule</th><th>Estado</th><th>Migrado (UTC)</th><th>Job</th><th>Cron</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
    </table>

    <div class="foot">Reporte de seguimiento &middot; solo lectura &middot; Migración Glue &rarr; EventBridge Scheduler</div>

<script>
const DATOS = __DATOS_JSON__;
let filtro = 'todos';

function esc(s){ if(s===null||s===undefined) return '&mdash;'; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function fila(d, i){
    return `
    <tr class="row" onclick="toggle(${i})">
        <td><span class="pill p-${d.disparo_estado}">${esc(d.disparo_estado)}</span></td>
        <td class="mono">${esc(d.schedule)}</td>
        <td class="est-${d.estado}">${esc(d.estado)}</td>
        <td class="mono">${esc(d.creado)}</td>
        <td class="mono">${esc(d.job)}</td>
        <td class="mono">${esc(d.cron)}</td>
    </tr>
    <tr class="detalle" id="det-${i}" style="display:none">
        <td colspan="6"><div class="detalle-inner">
            <table class="ptable">
                <tr><td class="pname">Trigger de origen</td><td class="pval">${esc(d.trigger_origen)}</td></tr>
                <tr><td class="pname">Schedule</td><td class="pval">${esc(d.schedule)}</td></tr>
                <tr><td class="pname">Creado (desactivado)</td><td class="pval">${esc(d.creado)}</td></tr>
                <tr><td class="pname">Activado (switch)</td><td class="pval">${esc(d.activado)}</td></tr>
                <tr><td class="pname">Estado schedule</td><td class="pval">${esc(d.estado)}</td></tr>
                <tr><td class="pname">Cron</td><td class="pval">${esc(d.cron)} (${esc(d.timezone)})</td></tr>
                <tr><td class="pname">Job</td><td class="pval">${esc(d.job)}</td></tr>
                <tr><td class="pname">Input (params)</td><td class="pval">${esc(d.input)}</td></tr>
                <tr><td class="pname">Role ARN</td><td class="pval">${esc(d.role_arn)}</td></tr>
                <tr><td class="pname">Disparo del job</td><td class="pval"><span class="pill p-${d.disparo_estado}">${esc(d.disparo_estado)}</span> ${esc(d.disparo_detalle)}</td></tr>
            </table>
        </div></td>
    </tr>`;
}

function toggle(i){ const e=document.getElementById('det-'+i); e.style.display = e.style.display==='none' ? '' : 'none'; }

function render(){
    const q = document.getElementById('buscador').value.toLowerCase();
    let html='', vis=0;
    DATOS.forEach((d,i)=>{
        let okF = (filtro==='todos') || (d.estado===filtro) || (d.disparo_estado===filtro);
        const texto = (d.schedule+' '+d.job+' '+d.cron+' '+d.trigger_origen).toLowerCase();
        if(okF && texto.includes(q)){ html+=fila(d,i); vis++; }
    });
    document.getElementById('tbody').innerHTML = html || '<tr><td colspan="6" class="vacio">Sin resultados.</td></tr>';
    document.getElementById('contador').textContent = vis+' de '+DATOS.length+' migrados';
}

document.getElementById('buscador').addEventListener('input', render);
document.querySelectorAll('.kpi').forEach(k=>k.addEventListener('click',()=>{
    document.querySelectorAll('.kpi').forEach(x=>x.classList.remove('active'));
    k.classList.add('active'); filtro=k.dataset.filtro; render();
}));
render();
</script>
</body>
</html>""".replace('__FECHA__', datetime.now(timezone.utc).strftime('%d-%m-%Y %H:%M UTC')) \
           .replace('__TOTAL__', str(total)) \
           .replace('__ENABLED__', str(enabled)) \
           .replace('__DISPARADOS__', str(disparados)) \
           .replace('__PENDIENTES__', str(pendientes)) \
           .replace('__DATOS_JSON__', datos_json)


# =============================================================================
# EJECUCIÓN
# =============================================================================

def correr(scheduler, glue):
    print("Leyendo schedules migrados (marca '[Migrado de Glue]' o sufijo -schedule)...")
    datos = construir_datos(scheduler, glue)
    print(f"Encontrados: {len(datos)} schedules migrados")
    if not datos:
        print("No hay schedules migrados todavía. (¿Ya corriste alguna migración real?)")
        return
    html_out = generar_html(datos)
    ruta = 'reporte_seguimiento.html'
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f"\n✅ Reporte generado: {ruta}")
    try:
        webbrowser.open(ruta)
    except Exception:
        pass


def main():
    args = parse_args()
    if args.demo:
        import boto3
        from moto import mock_aws

        @mock_aws
        def _demo():
            import time
            glue = boto3.client('glue', region_name=args.region)
            scheduler = boto3.client('scheduler', region_name=args.region)

            # Job de prueba
            glue.create_job(Name='sdlf-bigdata-aws-cost-monitor-psh-glue-job',
                            Role='arn:aws:iam::837538682169:role/R',
                            Command={'Name': 'pythonshell', 'ScriptLocation': 's3://x/cost.py', 'PythonVersion': '3'})
            glue.create_job(Name='sdlf-bigdata-backup-glue-job',
                            Role='arn:aws:iam::837538682169:role/R',
                            Command={'Name': 'glueetl', 'ScriptLocation': 's3://x/backup.py', 'PythonVersion': '3'})

            # Schedule migrado #1 (simulamos que YA se disparó: creamos un job run)
            scheduler.create_schedule(
                Name='sdlf-bigdata-aws-cost-monitor-glue-schedule',
                ScheduleExpression='cron(00 13 * * ? *)', ScheduleExpressionTimezone='UTC',
                FlexibleTimeWindow={'Mode': 'OFF'}, State='ENABLED',
                Description='[Migrado de Glue] ',
                Target={'Arn': 'arn:aws:scheduler:::aws-sdk:glue:startJobRun',
                        'RoleArn': 'arn:aws:iam::837538682169:role/SchedulerRole',
                        'Input': json.dumps({'JobName': 'sdlf-bigdata-aws-cost-monitor-psh-glue-job',
                                             'SecurityConfiguration': 'sdlf-bigdata-glue-security-config'})})
            # Simular que el job ya corrió (posterior a la creación)
            try:
                glue.start_job_run(JobName='sdlf-bigdata-aws-cost-monitor-psh-glue-job')
            except Exception:
                pass

            # Schedule migrado #2 (PENDIENTE: sin ejecuciones)
            scheduler.create_schedule(
                Name='sdlf-bigdata-backup-glue-schedule',
                ScheduleExpression='cron(0 2 * * ? *)', ScheduleExpressionTimezone='UTC',
                FlexibleTimeWindow={'Mode': 'OFF'}, State='ENABLED',
                Description='[Migrado de Glue] Backup nocturno',
                Target={'Arn': 'arn:aws:scheduler:::aws-sdk:glue:startJobRun',
                        'RoleArn': 'arn:aws:iam::837538682169:role/SchedulerRole',
                        'Input': json.dumps({'JobName': 'sdlf-bigdata-backup-glue-job'})})

            correr(scheduler, glue)

        _demo()
    else:
        import boto3
        from botocore.exceptions import NoCredentialsError, ClientError
        try:
            scheduler = boto3.client('scheduler', region_name=args.region)
            glue = boto3.client('glue', region_name=args.region)
            correr(scheduler, glue)
        except NoCredentialsError:
            print("No hay credenciales AWS. Configuralas o usa --demo.")
        except ClientError as e:
            print(f"Error de AWS: {e}")


def parse_args():
    p = argparse.ArgumentParser(description='Reporte HTML de seguimiento de migración (solo lectura)')
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--demo', action='store_true')
    return p.parse_args()


if __name__ == '__main__':
    main()
