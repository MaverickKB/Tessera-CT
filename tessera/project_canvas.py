"""Typed project pieces and relations, shared by the operator UI and agent tools."""
from copy import deepcopy
import math

KINDS = ('note', 'task', 'step', 'image', 'document', 'reference', 'group')
NODE_FIELDS = {'id', 'kind', 'title', 'body', 'x', 'y', 'width', 'height', 'z', 'parent',
               'card_id', 'step_id', 'session_slug', 'attachment_id', 'media_type', 'url', 'placed'}
LINK_FIELDS = {'id', 'from', 'to', 'label'}


def empty_canvas():
    return {'version': 0, 'nodes': [], 'links': []}


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('Canvas identifiers must be nonempty text of at most 256 characters')
    return value


def validate_canvas(canvas):
    if not isinstance(canvas, dict) or set(canvas) != {'version', 'nodes', 'links'}:
        raise ValueError('Invalid project canvas')
    if type(canvas['version']) is not int or canvas['version'] < 0:
        raise ValueError('Invalid canvas version')
    if not isinstance(canvas['nodes'], list) or not isinstance(canvas['links'], list):
        raise ValueError('Canvas nodes and links must be lists')
    nodes = {}
    for node in canvas['nodes']:
        if not isinstance(node, dict) or set(node) - NODE_FIELDS:
            raise ValueError('Unknown canvas object field')
        node_id = identifier(node.get('id'))
        if node_id in nodes or node.get('kind') not in KINDS:
            raise ValueError('Canvas object identity or kind is invalid')
        for key, value in node.items():
            if key == 'placed':
                if type(value) is not bool:
                    raise ValueError('Canvas placement must be boolean')
            elif key in ('x', 'y', 'width', 'height', 'z'):
                if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > 2 ** 53 - 1:
                    raise ValueError('Canvas geometry must be finite')
                if key in ('width', 'height') and value <= 0:
                    raise ValueError('Canvas dimensions must be positive')
            elif not isinstance(value, str) or len(value.encode()) > 8192:
                raise ValueError('Canvas text exceeds the project text limit')
        if node.get('url'):
            from .grafana import http_url
            http_url(node['url'])
        if node['kind'] == 'task' and not node.get('card_id'):
            raise ValueError('Task pieces must link to a board card')
        if node['kind'] == 'step' and not node.get('step_id'):
            raise ValueError('Plan pieces must link to a plan step')
        if node['kind'] in ('image', 'document'):
            from .channels import valid_name
            valid_name(node.get('session_slug'))
            attachment = node.get('attachment_id', '')
            if len(attachment) != 32 or any(c not in '0123456789abcdef' for c in attachment):
                raise ValueError('Media pieces must reference a stored attachment')
        nodes[node_id] = node
    for node in nodes.values():
        parent = node.get('parent')
        seen = {node['id']}
        while parent:
            if parent not in nodes or nodes[parent]['kind'] != 'group' or parent in seen:
                raise ValueError('Canvas groups must exist and cannot contain cycles')
            seen.add(parent)
            parent = nodes[parent].get('parent')
    links = set()
    for link in canvas['links']:
        if not isinstance(link, dict) or set(link) - LINK_FIELDS:
            raise ValueError('Unknown canvas connection field')
        link_id = identifier(link.get('id'))
        if link_id in links or link.get('from') not in nodes or link.get('to') not in nodes:
            raise ValueError('Canvas connections require unique identities and existing endpoints')
        if not isinstance(link.get('label', ''), str) or len(link.get('label', '').encode()) > 8192:
            raise ValueError('Invalid canvas connection label')
        links.add(link_id)


def apply_changes(current, changes, *, cards, steps):
    """Apply targeted patches to the latest state, preserving unrelated contributions."""
    if not isinstance(changes, dict) or set(changes) - {'nodes', 'links', 'remove_nodes', 'remove_links', 'expected_version'}:
        raise ValueError('Unknown canvas operation')
    if 'expected_version' in changes and changes['expected_version'] != current['version']:
        raise ValueError('Canvas changed; refresh before replacing this content')
    result = deepcopy(current)
    nodes = {node['id']: node for node in result['nodes']}
    links = {link['id']: link for link in result['links']}
    for key in ('nodes', 'links', 'remove_nodes', 'remove_links'):
        if not isinstance(changes.get(key, []), list):
            raise ValueError('Canvas changes must use lists')
    removed = set()
    for node_id in changes.get('remove_nodes', []):
        removed.add(identifier(node_id))
        nodes.pop(node_id, None)
    for node in nodes.values():
        if node.get('parent') in removed:
            node['parent'] = ''
    for patch in changes.get('nodes', []):
        if not isinstance(patch, dict):
            raise ValueError('Canvas object patches must be objects')
        node_id = identifier(patch.get('id'))
        if node_id in nodes:
            nodes[node_id].update(patch)
        else:
            nodes[node_id] = {'kind': 'note', 'title': '', 'body': '', 'x': 0, 'y': 0,
                              'width': 260, 'height': 180, 'z': 0, 'placed': True, **patch}
    for link_id in changes.get('remove_links', []):
        links.pop(identifier(link_id), None)
    for link_id, link in list(links.items()):
        if link['from'] in removed or link['to'] in removed:
            del links[link_id]
    for patch in changes.get('links', []):
        if not isinstance(patch, dict):
            raise ValueError('Canvas connection patches must be objects')
        link_id = identifier(patch.get('id'))
        links[link_id] = {**links.get(link_id, {}), **patch}
    result.update(nodes=list(nodes.values()), links=list(links.values()), version=current['version'] + 1)
    validate_canvas(result)
    changed_references = set()
    previous_ids = set()
    for node in current['nodes']:
        previous_ids.add(node['id'])
    for patch in changes.get('nodes', []):
        if any(key in patch for key in ('kind', 'card_id', 'step_id')) or patch['id'] not in previous_ids:
            changed_references.add(patch['id'])
    for node in result['nodes']:
        if node['id'] not in changed_references:
            continue
        if node['kind'] == 'task' and cards is not None and node['card_id'] not in cards:
            raise ValueError('Canvas task must reference a board card')
        if node['kind'] == 'step' and steps is not None and node['step_id'] not in steps:
            raise ValueError('Canvas step must belong to this project')
    return result


def planning_context(project, selected):
    """Only explicit project-chat turns receive this frame; board reads stay on demand."""
    import json
    chosen = []
    for node in project.get('canvas', empty_canvas())['nodes']:
        if node['id'] in selected:
            chosen.append(node)
    return ('Project workspace: ' + project['title'] + '\nProject ID: ' + project['id']
            + '\nWorkspace kind: ' + project.get('kind', 'planning')
            + '\nSelected pieces: ' + json.dumps(chosen, ensure_ascii=False)
            + '\nUse board_project_canvas to read or edit the persistent visual workspace. '
              'Choose the form that communicates best: notes, connected nodes for diagrams/maps, '
              'linked tasks/steps, stored images/documents, references or groups. '
              'Canvas task/step pieces link to the existing board records; use their board tools '
              'for work status. Canvas positions describe arrangement, not progress. '
              'Read the current canvas before editing existing content.')


def replay_canvas(records, project_id):
    """Replay deltas with independent per-project graph revisions."""
    canvases = {}
    sequence = 0
    for record in records:
        sequence += 1
        if type(record.get('sequence')) is not int or record['sequence'] != sequence:
            raise ValueError('Invalid canvas journal sequence')
        owner = identifier(record.get('project_id'))
        current = canvases.get(owner, empty_canvas())
        updated = apply_changes(current, record.get('changes'), cards=None, steps=None)
        if record.get('canvas_version') != updated['version']:
            raise ValueError('Invalid canvas journal version')
        canvases[owner] = updated
    return deepcopy(canvases.get(project_id, empty_canvas())), sequence
