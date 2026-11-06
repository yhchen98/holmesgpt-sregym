#!/usr/bin/env python3
"""Render runtime.json as offline HTML and the guide tool table. Requires Graphviz dot."""
import html
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from runtime.sources import git, pins


def render():
    data = json.loads((HERE / 'runtime.json').read_text())
    revisions = pins(ROOT)
    origins = {name: git(ROOT, 'config', '-f', '.gitmodules', '--get',
                         f'submodule.upstream/{name}.url').removesuffix('.git') for name in revisions}

    def source_link(source):
        repo, path, anchor = source['repo'], source['path'], source['anchor']
        href = '../../../' + path if repo == 'parent' else f'{origins[repo]}/blob/{revisions[repo]}/{path}'
        if anchor:
            href += '#' + anchor
        name = source.get('label', path)
        return (f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(name)}</a>'
                f'<small class="source-path">{html.escape(repo + ": " + path + (":" + anchor if anchor else ""))}</small>')

    def definition_block(tool):
        parts = [f'<dl class="tool-info"><dt>Toolset</dt><dd>{html.escape(tool["toolset"])}</dd>'
                 f'<dt>Execution</dt><dd>{html.escape(tool["route"])}</dd>'
                 f'<dt>Returns</dt><dd>{html.escape(tool["returns"])}</dd></dl>']
        for source in tool['sources']:
            content = subprocess.check_output(['git', '-C', str(ROOT / 'upstream' / source['repo']),
                'show', revisions[source['repo']] + ':' + source['path']], text=True)
            start, end = (int(part.removeprefix('L')) for part in source['anchor'].split('-'))
            excerpt = '\n'.join(content.splitlines()[start-1:end])
            parts.append(f'<p>{source_link(source)}</p><pre><code>{html.escape(excerpt)}</code></pre>')
        return (f'<details class="tool-definition"><summary>{html.escape(tool["name"])}</summary>'
                + ''.join(parts) + '</details>')

    q = lambda value: json.dumps(value, ensure_ascii=False)
    esc = html.escape
    dot = ['digraph runtime {', 'graph [rankdir=TB, nodesep=0.45, ranksep=0.95, '
           'splines=ortho, bgcolor="transparent", pad="0.6,0.5", outputorder=edgesfirst];',
           'node [shape=box, style="filled", fontname="Arial", fontsize=13, margin="0.14,0.12"];',
           'edge [fontname="Arial", fontsize=10, color="#68778a", arrowsize=0.65];']
    details = []
    for n in data['nodes']:
        palette = data['owners'][n['owner']]
        owner, fill, color = palette['label'], palette['fill'], palette['stroke']
        rows = [f'<TR><TD><FONT POINT-SIZE="10" COLOR="{color}">{esc(owner)}</FONT></TD></TR>',
                f'<TR><TD><B>{esc(n["title"])}</B></TD></TR>']
        rows += [f'<TR><TD><FONT POINT-SIZE="11" COLOR="#586577">{esc(line)}</FONT></TD></TR>' for line in n['lines']]
        label = '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1">' + ''.join(rows) + '</TABLE>'
        dot.append(f'{n["id"]} [id={q("node-"+n["id"])}, label=<{label}>, fillcolor={q(fill)}, '
                   f'color={q(color)}, group={q(n.get("column", ""))}, URL={q("#detail-"+n["id"])}, tooltip={q(n["purpose"])}];')
        fields = ''.join(f'<dt>{label}</dt><dd><ul>' +
                         ''.join(f'<li>{esc(item)}</li>' for item in n[key]) + '</ul></dd>'
                         for key, label in [('input', 'Input'), ('output', 'Output'), ('behavior', 'Behavior')])
        index = ''
        if n.get('tool_index'):
            groups = ''.join(f'<li><b>{esc(group["title"])}</b>: ' +
                             ', '.join(f'<code>{esc(t["name"])}</code>' for t in group['tool_definitions']) + '</li>'
                             for group in data['nodes'] if group.get('tool_definitions'))
            index = '<h3>Tools by implementation</h3><ul>' + groups + '</ul>'
        tool_blocks = ''.join(definition_block(tool) for tool in n.get('tool_definitions', []))
        if tool_blocks:
            tool_blocks = '<h3>Tool definitions</h3>' + tool_blocks
        references = ''.join(f'<li>{source_link(source)}</li>' for source in n['sources'])
        source_note = '<p class="source-note">Caller code for these backend requests.</p>' if n['owner'] == 'external' else ''
        references = f'<h3>Related Sources</h3>{source_note}<ul class="sources">{references}</ul>'
        details.append(f'<template id="detail-{n["id"]}" data-title="{esc(n["title"], quote=True)}">'
                       f'<div class="detail-body"><p class="owner">{esc(owner)}</p>'
                       f'<p>{esc(n["purpose"])}</p><dl>{fields}</dl>{index}{tool_blocks}{references}</div></template>')
    for row in data['rows']:
        dot.append('{rank=same; ' + '; '.join(row) + ';}')
        for a, b in zip(row, row[1:]):
            dot.append(f'{a} -> {b} [style=invis, weight=50];')
    # Invisible anchors order tiers only; they are not runtime relationships.
    for a, b in zip(data['rows'], data['rows'][1:]):
        dot.append(f'{a[0]} -> {b[0]} [style=invis, weight=100];')
    for e in data['edges']:
        attrs = dict(xlabel=e['label'])
        if e['kind'] == 'feedback':
            continue  # Draw this one outside return after native layout.
        if e['kind'] == 'exchange':
            attrs.update(dir='both', arrowtail='normal')
        elif e['kind'] == 'result':
            attrs.update(color='#2c5eaa')
        dot.append(f'{e["source"]} -> {e["target"]} [' + ', '.join(f'{k}={q(v)}' for k, v in attrs.items()) + '];')
    dot.append('}')
    svg = subprocess.run(['dot', '-Tsvg'], input='\n'.join(dot), text=True, check=True, stdout=subprocess.PIPE).stdout
    # Keep the one true feedback edge in an outside gutter, rather than routing it
    # through tool branches. Read the native box bounds; do not re-layout the graph.
    ns = '{http://www.w3.org/2000/svg}'
    document = ET.fromstring(svg)
    graph = document.find(ns + 'g')
    edge_data = {e['source'] + '->' + e['target']: e for e in data['edges']}
    for group in graph.findall(ns + 'g'):
        if group.get('class') != 'edge':
            continue
        dx, dy = edge_data[group.find(ns + 'title').text].get('label_offset', [0, 0])
        for text in group.findall(ns + 'text'):
            text.set('x', str(float(text.get('x')) + dx))
            text.set('y', str(float(text.get('y')) + dy))
        path = group.find(ns + 'path')
        path.set('data-link-line', '')
        underlay = ET.Element(ns + 'path', {'d': path.get('d'), 'fill': 'none',
            'stroke': 'white', 'stroke-width': '4', 'pointer-events': 'none', 'data-route-underlay': ''})
        group.insert(list(group).index(path), underlay)
    boxes = {}
    for group in graph.findall(ns + 'g'):
        if group.get('class') == 'node':
            points = [list(map(float, p.split(','))) for p in group.find('.//' + ns + 'polygon').get('points').split()]
            xs, ys = zip(*points)
            boxes[group.find(ns + 'title').text] = (min(xs), min(ys), max(xs), max(ys))
    translate_x = float(graph.get('transform').split('translate(')[1].split()[0])
    left = 24 - translate_x  # Inside SVG padding, outside all native routes.
    bottom = max(b[3] for b in boxes.values()) + 22
    a, b = boxes['observations'], boxes['request']
    sx, sy, tx, ty = (a[0]+a[2])/2, a[3], b[0], (b[1]+b[3])/2
    feedback_edge = next(e for e in data['edges'] if e['kind'] == 'feedback')
    label = feedback_edge['label']
    dx, dy = feedback_edge.get('label_offset', [0, 0])
    lx, ly = left - 8 + dx, (bottom + ty) / 2 + dy
    feedback = (f'<g class="feedback"><path data-link-line="" d="M {sx} {sy} V {bottom} H {left} V {ty} H {tx-7}" fill="none" stroke="#2c5eaa" stroke-width="1.5"/>'
                f'<polygon points="{tx},{ty} {tx-7},{ty-3} {tx-7},{ty+3}" fill="#2c5eaa"/>'
                f'<text x="{lx}" y="{ly}" text-anchor="middle" transform="rotate(-90 {lx} {ly})" font-family="Arial" font-size="11" fill="#2c5eaa">{esc(label)}</text></g>')
    graph.append(ET.fromstring(feedback.replace('<g ', '<g xmlns="http://www.w3.org/2000/svg" ', 1)))
    titles = {n['id']: n['title'] for n in data['nodes']}
    for group in graph.findall(ns + 'g'):
        if group.get('class') not in {'edge', 'feedback'}:
            continue
        edge = feedback_edge if group.get('class') == 'feedback' else edge_data[group.find(ns + 'title').text]
        group.set('tabindex', '0')
        group.set('role', 'group')
        group.set('aria-label', f"{titles[edge['source']]} to {titles[edge['target']]}: {edge['label']}")
        path = group.find(ns + 'path[@data-link-line]')
        hit = ET.Element(ns + 'path', {'d': path.get('d'), 'fill': 'none',
            'stroke': 'transparent', 'stroke-width': '8', 'pointer-events': 'stroke', 'aria-hidden': 'true'})
        group.insert(list(group).index(path), hit)
    document.set('role', 'group')
    document.set('aria-label', data['title'])
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    svg = ET.tostring(document, encoding='unicode')
    version = subprocess.check_output(['dot', '-V'], stderr=subprocess.STDOUT, text=True).strip()
    legend = ''.join(f'<span><i style="background:{esc(o["fill"], quote=True)};border-color:{esc(o["stroke"], quote=True)}"></i>{esc(o["label"])}</span>' for o in data['owners'].values())
    output = TEMPLATE.replace('@@TITLE@@', esc(data['title']))
    for marker, value in [('LEGEND', legend), ('SVG', svg),
                          ('DETAILS', '\n'.join(details)), ('VERSION', esc(version))]:
        output = output.replace('@@' + marker + '@@', value)
    (HERE / 'runtime-overview.html').write_text(output)
    guide = ROOT / 'docs/runtime.md'
    content = guide.read_text()
    start, end = '<!-- tool-inventory:start -->', '<!-- tool-inventory:end -->'
    before, inventory = content.split(start, 1)
    _, after = inventory.split(end, 1)
    table = ['| Tool | Toolset | Execution route | Returned information |', '| --- | --- | --- | --- |']
    for node in data['nodes']:
        for tool in node.get('tool_definitions', []):
            values = [f'`{tool["name"]}`', f'`{tool["toolset"]}`', tool['route'], tool['returns']]
            table.append('| ' + ' | '.join(v.replace('|', '&#124;') for v in values) + ' |')
    guide.write_text(before + start + '\n\n' + '\n'.join(table) + '\n\n' + end + after)
    print(f'Wrote {HERE / "runtime-overview.html"} ({version})')


TEMPLATE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title><meta name="generator" content="@@VERSION@@">
<style>
:root{color-scheme:light;font:15px/1.55 system-ui,sans-serif;color:#1e293b;background:#f6f8fb}
*{box-sizing:border-box}body{margin:0}main{max-width:1380px;margin:auto;padding:28px 32px 48px}
h1{font-size:27px;line-height:1.25;margin:0 0 8px}h2{font-size:21px}h3{font-size:16px}
p{margin:8px 0 14px}a{color:#285b9a;text-underline-offset:3px}
.legend{display:flex;flex-wrap:wrap;gap:18px;font-size:11px;letter-spacing:.04em;margin:20px 0 12px}
.legend span{display:flex;align-items:center;gap:6px}.legend i{width:12px;height:12px;border:1px solid;border-radius:3px}
figure{margin:0;padding:20px;background:white;border:1px solid #dce3ed;border-radius:12px}
svg{display:block;width:100%;height:auto}svg a:focus-visible polygon,svg a:focus-visible path{stroke:#c45a12;stroke-width:3}

.edge [data-link-line],.feedback [data-link-line],.edge polygon,.feedback polygon,.edge text,.feedback text{transition:stroke .12s,fill .12s,stroke-width .12s}
.edge:hover [data-link-line],.feedback:hover [data-link-line],.edge:focus-visible [data-link-line],.feedback:focus-visible [data-link-line]{stroke:#c45a12;stroke-width:2.5}
.edge:hover polygon,.feedback:hover polygon,.edge:focus-visible polygon,.feedback:focus-visible polygon{stroke:#c45a12;fill:#c45a12}
.edge:hover text,.feedback:hover text,.edge:focus-visible text,.feedback:focus-visible text{fill:#a3480e}
.edge:focus-visible,.feedback:focus-visible{outline:none}
@media(prefers-reduced-motion:reduce){.edge *,.feedback *{transition:none!important}}
.owner{font-size:11px;letter-spacing:.08em;color:#63738a}
dl{margin:12px 0}dt{font-size:13px;font-weight:700;margin-top:13px}dd{margin:3px 0 0}dd ul{margin:4px 0;padding-left:20px}li{margin:5px 0;overflow-wrap:anywhere}
.source-path{display:block;color:#586577;font-size:11px;overflow-wrap:anywhere}.source-note{font-size:12px;color:#586577}
.sources{padding-left:20px}.sources li{margin:10px 0}
.tool-definition{border-top:1px solid #dce3ed;padding:9px 0}.tool-definition summary{cursor:pointer;font-size:13px;font-weight:600}
pre{font:12px/1.5 ui-monospace,monospace;max-height:360px;overflow:auto;padding:12px;background:#f6f8fb;border:1px solid #dce3ed;border-radius:6px}
dialog{border:1px solid #cbd5e1;border-radius:12px;padding:0;max-width:640px;width:calc(100% - 40px);max-height:85vh;overflow:auto;color:#1e293b;background:white}
dialog::backdrop{background:#14213666}.card-header{position:sticky;top:0;display:flex;align-items:flex-start;gap:16px;padding:18px 24px 12px;background:white;border-bottom:1px solid #e3e8ef;z-index:1}.card-header h2{flex:1;margin:0}.card-header form{margin:0}#card-body{padding:8px 24px 24px}
button{font:inherit;border:1px solid #cbd5e1;border-radius:6px;background:#f3f6fa;padding:5px 11px;cursor:pointer}:focus-visible{outline:3px solid #598fca;outline-offset:3px}
@media(max-width:700px){main{padding:20px 12px}figure{padding:10px}h1{font-size:23px}}
@media print{dialog{display:none}main{padding:0}}
</style></head><body><main>
<h1>@@TITLE@@</h1>
<div class="legend" aria-label="Implementation ownership">@@LEGEND@@</div>
<figure>@@SVG@@</figure>
@@DETAILS@@
<dialog aria-labelledby="card-title"><header class="card-header"><h2 id="card-title"></h2><form method="dialog"><button>Close</button></form></header><div id="card-body"></div></dialog>
<script>
const dialog = document.querySelector('dialog');
let opener;
document.querySelector('figure').addEventListener('click', event => {
  const link = event.target.closest('a');
  const href = link?.getAttribute('href') || link?.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
  if (!href?.startsWith('#detail-') || !dialog.showModal) return;
  const detail = document.getElementById(href.slice(1));
  if (!detail) return;
  event.preventDefault();
  opener = link;
  document.getElementById('card-title').textContent = detail.dataset.title;
  document.getElementById('card-body').replaceChildren(detail.content.cloneNode(true));
  dialog.showModal();
  dialog.scrollTop = 0;
});
dialog.addEventListener('close', () => opener?.focus());
</script></main></body></html>
'''

if __name__ == '__main__':
    render()
