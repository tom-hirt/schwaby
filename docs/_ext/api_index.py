"""The ``api-index`` directive: every documented name, on one page.

Built at the end of the build from the objects autodoc registered, rather than
written by hand, so a method appears on the page the moment it is documented
and cannot be listed while it is not. A hand-kept list is sized against the
methods that existed when it was written, and silently falls behind.

Grouped by module, then by class, alphabetically at each level. Callables carry
``()``. Enum members are not listed one by one --- the streaming field enums
alone hold hundreds --- and each enum says how many members it has instead.
"""

import enum
import importlib

from docutils import nodes
from sphinx.util.docutils import SphinxDirective

CALLABLE = {'function', 'method', 'classmethod', 'staticmethod'}


class api_index(nodes.General, nodes.Element):
    """Placeholder, replaced once every page has been read."""


class ApiIndexDirective(SphinxDirective):
    def run(self):
        # The page's content depends on every other page, which Sphinx does
        # not know, so an incremental build would leave it stale. Re-read it
        # on every build.
        self.env.note_reread()
        return [api_index('')]


def _module_of(fullname, cache):
    """The longest importable module prefix of ``fullname``."""
    parts = fullname.split('.')
    for end in range(len(parts), 0, -1):
        candidate = '.'.join(parts[:end])
        if candidate not in cache:
            try:
                importlib.import_module(candidate)
                cache[candidate] = True
            except ImportError:
                cache[candidate] = False
        if cache[candidate]:
            return candidate
    return parts[0]


def _resolve(fullname, module):
    """The live object, or None; used only to recognise enum members."""
    try:
        obj = importlib.import_module(module)
        for part in fullname[len(module) + 1:].split('.'):
            obj = getattr(obj, part)
        return obj
    except Exception:                                  # noqa: BLE001
        return None


def _entries(env):
    """(module, qualified name, objtype, docname, anchor) per documented name.

    Enum members are dropped here and counted onto their enum instead.
    """
    cache = {}
    rows = []
    members = {}
    for fullname, entry in env.get_domain('py').objects.items():
        if entry.aliased or entry.objtype == 'module':
            continue
        module = _module_of(fullname, cache)
        qualname = fullname[len(module) + 1:]
        if not qualname:
            continue
        if entry.objtype == 'attribute':
            if isinstance(_resolve(fullname, module), enum.Enum):
                owner = fullname.rsplit('.', 1)[0]
                members[owner] = members.get(owner, 0) + 1
                continue
        rows.append((module, qualname, entry.objtype, entry.docname,
                     entry.node_id, fullname))
    return rows, members


def _item(app, fromdocname, text, docname, anchor, suffix=''):
    uri = app.builder.get_relative_uri(fromdocname, docname) + '#' + anchor
    para = nodes.paragraph()
    ref = nodes.reference('', '', internal=True, refuri=uri)
    ref += nodes.literal(text, text)
    para += ref
    if suffix:
        para += nodes.Text(suffix)
    return nodes.list_item('', para)


def _build(app, fromdocname):
    rows, members = _entries(app.env)
    by_module = {}
    for row in rows:
        by_module.setdefault(row[0], []).append(row)

    section = nodes.bullet_list()
    for module in sorted(by_module):
        mod_item = nodes.list_item('', nodes.paragraph(
            '', '', nodes.literal(module, module)))
        # A name's parent is the documented name one dot shorter, if there is
        # one; nested classes such as Client.Quote.Fields hang off Client.Quote
        # only if Client.Quote itself was documented, and off Client otherwise.
        known = {r[1] for r in by_module[module]}
        children = {}
        for row in by_module[module]:
            qual = row[1]
            parent = qual.rsplit('.', 1)[0] if '.' in qual else None
            while parent is not None and parent not in known:
                parent = parent.rsplit('.', 1)[0] if '.' in parent else None
            children.setdefault(parent, []).append(row)

        def render(parent):
            kids = sorted(children.get(parent, []),
                          key=lambda r: r[1].lower())
            if not kids:
                return None
            lst = nodes.bullet_list()
            for _, qual, objtype, docname, anchor, fullname in kids:
                text = qual + ('()' if objtype in CALLABLE else '')
                count = members.get(fullname)
                suffix = (' ({} member{})'.format(count, '' if count == 1
                                                  else 's')
                          if count else '')
                item = _item(app, fromdocname, text, docname, anchor, suffix)
                sub = render(qual)
                if sub is not None:
                    item += sub
                lst += item
            return lst

        tree = render(None)
        if tree is not None:
            mod_item += tree
        section += mod_item
    return section


def process_api_index(app, doctree, fromdocname):
    for node in list(doctree.findall(api_index)):
        node.replace_self(_build(app, fromdocname))


def setup(app):
    app.add_node(api_index)
    app.add_directive('api-index', ApiIndexDirective)
    app.connect('doctree-resolved', process_api_index)
    return {'parallel_read_safe': True, 'parallel_write_safe': True}
