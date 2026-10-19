"""Read-only SVG/HTML views of an independently checked, saved GAP result."""

from html import escape

from .models import Gap
from .verify import verify


def view(result):
    problem = Gap.model_validate(result.get('problem') or result['approved_problem'])
    solver = result['solver']
    candidate = solver.get('candidate')
    check = verify(problem, candidate) if candidate is not None else None
    # Never draw an incumbent that fails independent arithmetic or differs from
    # the persisted verification used to authorize the result.
    if check != result.get('verification') or (check and not check['valid']):
        raise ValueError('saved result does not match independent verification')
    assignments = []
    for item in candidate['assignments'] if check else []:
        i, j = problem.resources.index(item['resource']), problem.tasks.index(item['task'])
        assignments.append(dict(**item, cost=problem.costs[i][j], consumption=problem.consumption[i][j]))
    resources = []
    for resource, capacity in zip(problem.resources, problem.capacities):
        load = check['loads'][resource] if check else None
        resources.append(dict(resource=resource, capacity=capacity, load=load,
            available=capacity-load if load is not None else None,
            percent=100*load/capacity if capacity and load is not None else None,
            tasks=[a['task'] for a in assignments if a['resource'] == resource]))
    return dict(problem=problem.model_dump(), status=solver['raw_status'],
        objective=check['recomputed_objective'] if check else None,
        optimality_proven=bool(result['optimality_proven'] and solver['solver_proof'] and check),
        verification=check, assignments=assignments, resources=resources,
        seconds=solver.get('solver_wall_seconds'), branch=result.get('branch', 'HITL'),
        approval_actor=result.get('approval_actor'))


def svg(result):
    data = view(result)
    p = data['problem']
    esc = lambda value: escape(str(value), quote=True)
    def label(value, x, y):
        sizing = ' textLength="240" lengthAdjust="spacingAndGlyphs"' if len(value) > 28 else ''
        return f'<text x="{x}" y="{y}"{sizing}>{esc(value)}</text>'
    height = max(260, 100 + 72 * max(len(p['tasks']), len(p['resources'])))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Affectations CP-SAT vérifiées</title>',
        f'<desc id="desc">Statut {esc(data["status"])}. Coût {esc(data["objective"])} {esc(p["cost_unit"])}.</desc>',
        '<style>text{font:15px system-ui;fill:#173645}.link{stroke:#138876;stroke-width:3;fill:none}.assignment:hover .link,.assignment:focus .link{stroke:#e48b1d;stroke-width:7}.node{fill:#eff7f6;stroke:#138876}.resource:hover .node,.resource:focus .node{fill:#e3b85c}.detail{visibility:hidden}.assignment:hover .detail,.assignment:focus .detail,.resource:hover .detail,.resource:focus .detail{visibility:visible}g:focus{outline:none}</style>',
        '<text x="30" y="30">Tâches</text><text x="720" y="30">Ressources</text>']
    for i in range(len(p['resources'])):
        parts.append(f'<style>svg:has(.resource-r{i}:hover) .to-r{i},svg:has(.resource-r{i}:focus) .to-r{i}'
                     '{stroke:#e48b1d;stroke-width:7}</style>')
    if data['verification']:
        for a in data['assignments']:
            j, i = p['tasks'].index(a['task']), p['resources'].index(a['resource'])
            y, ry = 82+j*72, 82+i*72
            detail = f'{a["task"]} : {a["resource"]}; coût {a["cost"]} {p["cost_unit"]}; consommation {a["consumption"]} {p["resource_unit"]}'
            parts += [f'<g class="assignment" tabindex="0"><title>{esc(detail)}</title>',
                f'<path class="link to-r{i}" d="M 300 {y} C 470 {y}, 530 {ry}, 700 {ry}"/>',
                f'<rect class="node" x="30" y="{y-24}" width="270" height="48" rx="9"/>',
                label(a['task'], 43, y+5),
                f'<text class="detail" x="43" y="{y+43}">Coût : {a["cost"]} · Charge : {a["consumption"]}</text></g>']
    else:
        parts.append(f'<text x="30" y="85">{esc(data["status"])} : aucune affectation vérifiée disponible.</text>')
    for i, r in enumerate(data['resources']):
        y = 82+i*72
        detail = f'{r["resource"]}: {r["load"]}/{r["capacity"]} {p["resource_unit"]}; tâches: {", ".join(r["tasks"]) or "aucune"}'
        parts += [f'<g class="resource resource-r{i}" tabindex="0"><title>{esc(detail)}</title>',
            f'<rect class="node" x="700" y="{y-24}" width="270" height="48" rx="9"/>',
            label(r['resource'], 713, y+5),
            f'<text class="detail" x="713" y="{y+43}">{esc(r["load"])} / {r["capacity"]} · {len(r["tasks"])} tâche(s)</text></g>']
    return ''.join(parts) + '</svg>'


def html(result):
    data = view(result)
    p = data['problem']
    e = lambda value: escape(str(value), quote=True)
    rows = ''.join('<tr>' + ''.join(f'<td>{e(a[k])}</td>' for k in ('task','resource','cost','consumption')) + '</tr>' for a in data['assignments'])
    bars = []
    for r in data['resources']:
        percent = f'{r["percent"]:.2f} %' if r['percent'] is not None else 'sans ratio (capacité nulle ou absence de candidat)'
        bars.append(f'<section><h3>{e(r["resource"])}</h3><p>{e(r["load"])} / {r["capacity"]} {e(p["resource_unit"])} · {percent} · disponible : {e(r["available"])}</p>'
            f'<meter min="0" max="{max(1,r["capacity"])}" value="{r["load"] or 0}" aria-label="Charge de {e(r["resource"])}"></meter><p>Tâches : {e(", ".join(r["tasks"]) or "aucune")}</p></section>')
    return ('<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>OR-Agent · Résultat</title><style>body{font:16px system-ui;color:#173645;max-width:1100px;margin:24px auto;padding:0 16px}svg{width:100%;min-width:0}meter{width:100%;height:26px}section{border-top:1px solid #cedbdc;padding:8px 0}table{border-collapse:collapse;width:100%}td,th{padding:9px;text-align:left;border-bottom:1px solid #cedbdc}p,td{overflow-wrap:anywhere}.table,.graph{overflow:auto}@media(max-width:600px){.graph svg{width:800px}}</style>'
        f'<h1>{e(data["status"])} · {e(data["objective"])} {e(p["cost_unit"])}</h1>'
        f'<p>Branche : {e(data["branch"])} · Approbation : {e(data["approval_actor"] or "non nécessaire (déterministe)")} · Optimalité prouvée : {"oui" if data["optimality_proven"] else "non"}</p>'
        f'<p>Vérification indépendante : {"positive" if data["verification"] else "sans candidat"} · Temps CP-SAT : {e(data["seconds"])} s · {len(p["tasks"])} tâches · {len(p["resources"])} ressources.</p>'
        '<p>Résultat relatif au GAP reconstruit ou explicitement approuvé. Aucun certificat universel de fidélité de la prose.</p>'
        + '<div class="graph">' + svg(result) + '</div><p>Survolez ou sélectionnez une tâche ou ressource pour ses détails. Sur écran étroit, faites défiler le graphe horizontalement.</p><h2>Utilisation des ressources</h2>' + ''.join(bars)
        + f'<h2>Détail des affectations</h2><div class="table"><table><thead><tr><th>Tâche</th><th>Ressource</th><th>Coût ({e(p["cost_unit"])})</th><th>Consommation ({e(p["resource_unit"])})</th></tr></thead><tbody>{rows}</tbody></table></div></html>')
