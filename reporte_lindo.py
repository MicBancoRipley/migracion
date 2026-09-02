"""
=============================================================================
REPORTE HTML (TARJETAS DETALLADAS + INTERACCIÓN): Glue -> EventBridge Scheduler
=============================================================================

Genera UN SOLO archivo HTML autocontenido con:
  - Una TARJETA GRANDE por trigger, con tabla de parámetros lado a lado
    (Job, Periodo, Cron, Ruta S3, Estado, Argumentos, Timeout, SecurityConfig)
    y un ✓ / → por cada parámetro (preservado 1:1 o transformado).
  - Buscador en vivo (por trigger, job, ruta, periodo...).
  - Filtros por estado del mapeo (Todos / OK / Revisar / No mapeable).
  - KPIs arriba (clickeables para filtrar).

Las tarjetas se mantienen grandes y detalladas; la interacción sirve para
NAVEGAR entre muchas (mostrar/ocultar según búsqueda y filtro).

Pensado para MANDAR por chat/correo y explorar en pantalla.
Es 100% SOLO LECTURA. No crea, modifica ni borra nada.
Permisos: glue:GetTriggers, glue:GetJob  (solo lectura)

USO:
    python reporte_lindo.py --demo
    python reporte_lindo.py --region us-east-1
    # Genera: reporte_mapeo.html  (un solo archivo)

=============================================================================
"""

import html
import json
import argparse
import webbrowser
from datetime import datetime


PREFIJO = 'sdlf-bigdata'
TIMEZONE = 'UTC'


# =============================================================================
# TRADUCIR CRON A LENGUAJE HUMANO (el "periodo")
# =============================================================================

DIAS = {'MON': 'lunes', 'TUE': 'martes', 'WED': 'miércoles', 'THU': 'jueves',
        'FRI': 'viernes', 'SAT': 'sábado', 'SUN': 'domingo',
        '1': 'domingo', '2': 'lunes', '3': 'martes', '4': 'miércoles',
        '5': 'jueves', '6': 'viernes', '7': 'sábado'}


def cron_a_humano(expr):
    if not expr:
        return "(sin periodo)"
    try:
        if expr.startswith('rate('):
            return expr.replace('rate(', 'cada ').rstrip(')')
        if not expr.startswith('cron('):
            return expr
        campos = expr[len('cron('):-1].split()
        if len(campos) != 6:
            return expr
        minuto, hora, dia_mes, mes, dia_sem, anio = campos
        try:
            hh = int(hora) if hora not in ('*', '?') else None
            mm = int(minuto) if minuto not in ('*', '?') else 0
            hora_txt = f"{hh:02d}:{mm:02d}" if hh is not None else None
        except ValueError:
            hora_txt = None
        if dia_sem not in ('*', '?'):
            nombres = [DIAS.get(d.strip().upper(), d) for d in dia_sem.replace('-', ',').split(',')]
            cuando = "los " + " y ".join(nombres)
        elif dia_mes not in ('*', '?'):
            cuando = f"el día {dia_mes} de cada mes"
        else:
            cuando = "todos los días"
        if hora_txt:
            return f"{cuando} a las {hora_txt}"
        return f"{cuando}"
    except Exception:
        return expr


# =============================================================================
# LECTURA (solo lectura)
# =============================================================================

def obtener_triggers_del_prefijo(glue_client, prefijo):
    triggers = []
    next_token = None
    while True:
        kwargs = {'MaxResults': 200}
        if next_token:
            kwargs['NextToken'] = next_token
        resp = glue_client.get_triggers(**kwargs)
        triggers.extend(resp['Triggers'])
        next_token = resp.get('NextToken')
        if not next_token:
            break
    return [t for t in triggers
            if t['Type'] == 'SCHEDULED' and t['Name'].startswith(prefijo)]


def obtener_ruta_script(glue_client, job_name, cache):
    if not job_name:
        return "(sin job)"
    if job_name in cache:
        return cache[job_name]
    try:
        job = glue_client.get_job(JobName=job_name)
        ruta = job['Job'].get('Command', {}).get('ScriptLocation', '(sin ScriptLocation)')
    except Exception as e:
        ruta = f"(no se pudo leer: {type(e).__name__})"
    cache[job_name] = ruta
    return ruta


# =============================================================================
# CONSTRUIR DATOS
# =============================================================================

def construir_datos(glue_client, triggers):
    datos = []
    cache = {}
    for t in triggers:
        actions = t.get('Actions', [])
        primer = actions[0] if actions else {}
        job_name = primer.get('JobName')
        crawler_name = primer.get('CrawlerName')
        ruta = obtener_ruta_script(glue_client, job_name, cache) if job_name else "(activa crawler)"

        estado_glue = t.get('State', 'CREATED')
        estado_sched = 'ENABLED' if estado_glue in ('ACTIVATED', 'CREATED') else 'DISABLED'
        nombre_sched = t['Name'][:-8] + '-schedule' if t['Name'].endswith('-trigger') else t['Name'] + '-schedule'

        problemas = []
        if crawler_name and not job_name:
            problemas.append("Activa un CRAWLER (no mapeable con StartJobRun)")
        if len(actions) > 1:
            problemas.append(f"Activa {len(actions)} jobs (necesita 1 schedule por job)")
        if not t.get('Schedule'):
            problemas.append("Sin expresión de periodo (cron)")

        if any('CRAWLER' in p for p in problemas):
            estado_mapeo = 'no_mapeable'
        elif problemas:
            estado_mapeo = 'revisar'
        else:
            estado_mapeo = 'ok'

        # Parámetros lado a lado: (nombre, antes, despues)
        params = [
            ('Job',           job_name or crawler_name or '—', job_name or crawler_name or '—'),
            ('Periodo',       cron_a_humano(t.get('Schedule')), cron_a_humano(t.get('Schedule'))),
            ('Cron',          t.get('Schedule', '—'), t.get('Schedule', '—')),
            ('Ruta script',   ruta, ruta),
            ('Estado',        estado_glue, estado_sched),
            ('Argumentos',    json.dumps(primer.get('Arguments', {}), ensure_ascii=False) if primer.get('Arguments') else '—',
                              json.dumps(primer.get('Arguments', {}), ensure_ascii=False) if primer.get('Arguments') else '—'),
            ('Timeout',       primer.get('Timeout', '—'), primer.get('Timeout', '—')),
            ('SecurityConfig', primer.get('SecurityConfiguration', '—'), primer.get('SecurityConfiguration', '—')),
            ('Descripción',   (t.get('Description') or '—'), (t.get('Description') or '—')),
        ]

        descripcion = t.get('Description', '').strip()

        datos.append({
            'trigger': t['Name'],
            'schedule': nombre_sched,
            'job': job_name or crawler_name or '',
            'periodo': cron_a_humano(t.get('Schedule')),
            'cron': t.get('Schedule', ''),
            'ruta': ruta,
            'descripcion': descripcion,
            'estado_mapeo': estado_mapeo,
            'params': params,
            'problemas': problemas,
        })
    return datos


# =============================================================================
# GENERAR HTML (autocontenido: datos embebidos como JSON, render en JS)
# =============================================================================

def generar_html(datos):
    ok = sum(1 for d in datos if d['estado_mapeo'] == 'ok')
    rev = sum(1 for d in datos if d['estado_mapeo'] == 'revisar')
    no = sum(1 for d in datos if d['estado_mapeo'] == 'no_mapeable')
    con_desc = sum(1 for d in datos if d.get('descripcion'))
    datos_json = json.dumps(datos, ensure_ascii=False)

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte de Mapeo - Glue &rarr; EventBridge Scheduler</title>
<style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f1f5f9; color:#0f172a; margin:0; padding:24px; }
    h1 { font-size:22px; margin:0 0 4px; }
    .sub { color:#64748b; margin-bottom:20px; font-size:14px; }
    .kpis { display:flex; gap:12px; margin-bottom:18px; flex-wrap:wrap; }
    .kpi { background:#fff; border-radius:10px; padding:14px 20px; box-shadow:0 1px 3px rgba(0,0,0,.08); flex:1; min-width:120px; cursor:pointer; border:2px solid transparent; transition:.15s; }
    .kpi:hover { border-color:#cbd5e1; }
    .kpi.active { border-color:#0ea5e9; }
    .kpi .n { font-size:28px; font-weight:700; }
    .kpi .l { color:#64748b; font-size:12px; }
    .controls { display:flex; gap:12px; margin-bottom:18px; flex-wrap:wrap; align-items:center; position:sticky; top:0; background:#f1f5f9; padding:8px 0; z-index:10; }
    #buscador { flex:1; min-width:220px; padding:10px 14px; border:1px solid #cbd5e1; border-radius:8px; font-size:14px; }
    .count { color:#64748b; font-size:13px; white-space:nowrap; }
    /* Tarjetas grandes */
    .card { background:#fff; border-radius:12px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.1); overflow:hidden; }
    .card-head { display:flex; justify-content:space-between; align-items:center; padding:16px 20px; background:#f8fafc; }
    .tname { font-weight:700; font-size:15px; }
    .sname { color:#0ea5e9; font-size:13px; margin-top:2px; }
    .badge { color:#fff; padding:6px 14px; border-radius:999px; font-size:13px; font-weight:600; white-space:nowrap; }
    .b-ok { background:#16a34a; } .b-revisar { background:#d97706; } .b-no_mapeable { background:#dc2626; }
    .lb-ok { border-left:6px solid #16a34a; } .lb-revisar { border-left:6px solid #d97706; } .lb-no_mapeable { border-left:6px solid #dc2626; }
    .ptable { width:100%; border-collapse:collapse; }
    .ptable th { text-align:left; font-size:11px; color:#64748b; padding:8px 20px; border-bottom:1px solid #e2e8f0; text-transform:uppercase; }
    .ptable td { padding:8px 20px; border-bottom:1px solid #f1f5f9; font-size:14px; vertical-align:top; }
    .pname { font-weight:600; color:#475569; width:150px; }
    .pval { font-family:ui-monospace, monospace; font-size:13px; word-break:break-all; }
    .arrow { text-align:center; font-weight:700; width:34px; }
    .a-ok { color:#16a34a; } .a-chg { color:#d97706; }
    .problemas { background:#fef3c7; padding:12px 20px; font-size:13px; }
    .problemas ul { margin:6px 0 0; padding-left:18px; }
    .descripcion { background:#eff6ff; border-bottom:1px solid #dbeafe; padding:12px 20px; font-size:13px; color:#1e3a8a; }
    .foot { color:#94a3b8; font-size:12px; margin-top:20px; text-align:center; }
    .vacio { text-align:center; color:#94a3b8; padding:40px; }
</style>
</head>
<body>
    <h1>Reporte de Mapeo: Glue Triggers &rarr; EventBridge Scheduler</h1>
    <div class="sub">Prefijo <b>__PREFIJO__</b> &middot; Generado el __FECHA__ &middot; Solo lectura (no se migró nada)</div>

    <div class="kpis">
        <div class="kpi active" data-filtro="todos"><div class="n">__TOTAL__</div><div class="l">Total triggers</div></div>
        <div class="kpi" data-filtro="ok"><div class="n" style="color:#16a34a">__OK__</div><div class="l">&#10003; Mapeo 1:1 OK</div></div>
        <div class="kpi" data-filtro="revisar"><div class="n" style="color:#d97706">__REV__</div><div class="l">&#9888; Revisar</div></div>
        <div class="kpi" data-filtro="no_mapeable"><div class="n" style="color:#dc2626">__NO__</div><div class="l">&#10007; No mapeable</div></div>
        <div class="kpi" data-filtro="con_desc"><div class="n" style="color:#2563eb">__CONDESC__</div><div class="l">&#128221; Con descripción</div></div>
    </div>

    <div class="controls">
        <input id="buscador" type="text" placeholder="Buscar por trigger, job, ruta, periodo...">
        <span class="count" id="contador"></span>
    </div>

    <div id="cards"></div>

    <div class="foot">Reporte autocontenido de solo lectura &middot; Migración Glue &rarr; EventBridge Scheduler</div>

<script>
const DATOS = __DATOS_JSON__;
let filtroEstado = 'todos';
const BADGE = { ok: '&#10003; 1:1 OK', revisar: '&#9888; Revisar', no_mapeable: '&#10007; No mapeable' };

function esc(s) {
    if (s === null || s === undefined) return '&mdash;';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function tarjeta(d) {
    let filas = '';
    for (const [nombre, antes, despues] of d.params) {
        const igual = String(antes) === String(despues);
        const check = igual ? '&#10003;' : '&rarr;';
        const cls = igual ? 'a-ok' : 'a-chg';
        filas += `<tr>
            <td class="pname">${esc(nombre)}</td>
            <td class="pval">${esc(antes)}</td>
            <td class="arrow ${cls}">${check}</td>
            <td class="pval">${esc(despues)}</td>
        </tr>`;
    }
    let prob = '';
    if (d.problemas.length) {
        prob = '<div class="problemas"><b>Atención:</b><ul>' +
               d.problemas.map(p => '<li>'+esc(p)+'</li>').join('') + '</ul></div>';
    }
    // Banner de descripción destacado (puede tener info importante del equipo)
    let descBanner = '';
    if (d.descripcion) {
        descBanner = '<div class="descripcion"><b>&#128221; Descripción del trigger:</b> ' + esc(d.descripcion) + '</div>';
    }
    return `<div class="card">
        <div class="card-head lb-${d.estado_mapeo}">
            <div>
                <div class="tname">${esc(d.trigger)}</div>
                <div class="sname">&rarr; ${esc(d.schedule)}</div>
            </div>
            <span class="badge b-${d.estado_mapeo}">${BADGE[d.estado_mapeo]}</span>
        </div>
        ${descBanner}
        <table class="ptable">
            <thead><tr><th>Parámetro</th><th>Glue Trigger (antes)</th><th></th><th>EventBridge Schedule (después)</th></tr></thead>
            <tbody>${filas}</tbody>
        </table>
        ${prob}
    </div>`;
}

function render() {
    const q = document.getElementById('buscador').value.toLowerCase();
    const cont = document.getElementById('cards');
    let visibles = 0, html = '';
    DATOS.forEach(d => {
        let coincideEstado;
        if (filtroEstado === 'todos') coincideEstado = true;
        else if (filtroEstado === 'con_desc') coincideEstado = !!d.descripcion;
        else coincideEstado = (d.estado_mapeo === filtroEstado);
        const texto = (d.trigger + ' ' + d.job + ' ' + d.ruta + ' ' + d.periodo + ' ' + d.cron + ' ' + (d.descripcion||'')).toLowerCase();
        const coincideBusqueda = texto.includes(q);
        if (coincideEstado && coincideBusqueda) { html += tarjeta(d); visibles++; }
    });
    cont.innerHTML = html || '<div class="vacio">No hay triggers que coincidan con el filtro/búsqueda.</div>';
    document.getElementById('contador').textContent = visibles + ' de ' + DATOS.length + ' triggers';
}

document.getElementById('buscador').addEventListener('input', render);
document.querySelectorAll('.kpi').forEach(kpi => {
    kpi.addEventListener('click', () => {
        document.querySelectorAll('.kpi').forEach(k => k.classList.remove('active'));
        kpi.classList.add('active');
        filtroEstado = kpi.dataset.filtro;
        render();
    });
});
render();
</script>
</body>
</html>""".replace('__PREFIJO__', html.escape(PREFIJO)) \
           .replace('__FECHA__', datetime.now().strftime('%d-%m-%Y %H:%M')) \
           .replace('__TOTAL__', str(len(datos))) \
           .replace('__OK__', str(ok)) \
           .replace('__REV__', str(rev)) \
           .replace('__NO__', str(no)) \
           .replace('__CONDESC__', str(con_desc)) \
           .replace('__DATOS_JSON__', datos_json)


# =============================================================================
# EJECUCIÓN
# =============================================================================

def correr(glue_client):
    print(f"Buscando triggers SCHEDULED con prefijo '{PREFIJO}'...")
    triggers = obtener_triggers_del_prefijo(glue_client, PREFIJO)
    print(f"Encontrados: {len(triggers)}")
    if not triggers:
        print("No hay triggers que reportar.")
        return
    print("Leyendo rutas de scripts de los jobs (get_job)...")
    datos = construir_datos(glue_client, triggers)
    html_out = generar_html(datos)
    ruta = 'reporte_mapeo.html'
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f"\n✅ Reporte generado: {ruta}")
    print("   Es UN SOLO archivo. Ábrelo con doble-click o mándaselo a tu jefe.")
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
            glue = boto3.client('glue', region_name=args.region)
            jobs = {
                'sdlf-bigdata-abr-historico-glue-job': 's3://sdlf-bigdata-scripts/abr_historico.py',
                'sdlf-bigdata-backup-glue-job': 's3://sdlf-bigdata-scripts/backup.py',
                'sdlf-bigdata-ventas-glue-job': 's3://sdlf-bigdata-scripts/ventas.py',
                'sdlf-bigdata-multi-a-glue-job': 's3://sdlf-bigdata-scripts/multi_a.py',
                'sdlf-bigdata-multi-b-glue-job': 's3://sdlf-bigdata-scripts/multi_b.py',
            }
            for name, ruta in jobs.items():
                glue.create_job(Name=name, Role='arn:aws:iam::111111111111:role/R',
                                Command={'Name': 'glueetl', 'ScriptLocation': ruta, 'PythonVersion': '3'})
            glue.create_crawler(Name='sdlf-bigdata-crawler', Role='arn:aws:iam::111111111111:role/R',
                                DatabaseName='db', Targets={'S3Targets': [{'Path': 's3://x/'}]})
            glue.create_trigger(Name='sdlf-bigdata-abr-historico-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(30 17 * * ? *)',
                                Description='IMPORTANTE: no cambiar el horario, depende del cierre contable diario.',
                                Actions=[{'JobName': 'sdlf-bigdata-abr-historico-glue-job', 'Arguments': {'--modo': 'full'}, 'Timeout': 2880}])
            glue.create_trigger(Name='sdlf-bigdata-backup-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 2 ? * MON *)',
                                Description='Backup semanal. Coordinar con equipo de infraestructura antes de tocar.',
                                Actions=[{'JobName': 'sdlf-bigdata-backup-glue-job'}])
            glue.create_trigger(Name='sdlf-bigdata-ventas-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 6 1 * ? *)',
                                Actions=[{'JobName': 'sdlf-bigdata-ventas-glue-job'}])
            glue.create_trigger(Name='sdlf-bigdata-multi-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 5 * * ? *)',
                                Actions=[{'JobName': 'sdlf-bigdata-multi-a-glue-job'},
                                         {'JobName': 'sdlf-bigdata-multi-b-glue-job'}])
            glue.create_trigger(Name='sdlf-bigdata-crawler-glue-trigger', Type='SCHEDULED',
                                Schedule='cron(0 11 1 * ? *)',
                                Actions=[{'CrawlerName': 'sdlf-bigdata-crawler'}])
            correr(glue)

        _demo()
    else:
        import boto3
        from botocore.exceptions import NoCredentialsError, ClientError
        try:
            glue = boto3.client('glue', region_name=args.region)
            correr(glue)
        except NoCredentialsError:
            print("No hay credenciales AWS. Configuralas o usa --demo.")
        except ClientError as e:
            code = e.response['Error']['Code']
            if code == 'AccessDeniedException':
                print("Sin permisos. Necesitas: glue:GetTriggers y glue:GetJob")
            else:
                print(f"Error de AWS: {e}")


def parse_args():
    p = argparse.ArgumentParser(description='Reporte HTML con tarjetas detalladas + filtro (solo lectura)')
    p.add_argument('--region', default='us-east-1')
    p.add_argument('--demo', action='store_true')
    return p.parse_args()


if __name__ == '__main__':
    main()
