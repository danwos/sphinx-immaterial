"""Injects mkdocs-style `nav` and `page` objects into the HTML jinja2 context."""

import copy
import os
import re
from typing import List, Union, NamedTuple, Optional, Tuple, Iterator, Dict
import urllib.parse
import docutils.nodes
import markupsafe
import sphinx.builders
import sphinx.application
import sphinx.environment.adapters.toctree


# env var is only defined in RTD hosted builds
READTHEDOCS = os.getenv("READTHEDOCS")


def _strip_fragment(url: str) -> str:
    """Returns the url with any fragment identifier removed."""
    fragment_start = url.find("#")
    if fragment_start == -1:
        return url
    return url[:fragment_start]


def _insert_wbr(text: str) -> str:
    """Inserts <wbr> tags after likely split points for API symbols."""
    # Split after punctuation
    text = re.sub("([.:_-]+)", r"\1<wbr>", text)
    # Split before brackets
    text = re.sub(r"([(\[{])", r"<wbr>\1", text)
    # Split between camel-case words
    text = re.sub(r"([a-z])([A-Z])", r"\1<wbr>\2", text)
    return text


class MkdocsNavEntry:
    # Title to display, as HTML.
    title: str

    # Aria label text, plain text.
    aria_label: str = None

    # URL of this page, or the first descendent if `caption_only` is `True`.
    url: Optional[str]
    # List of children
    children: List["MkdocsNavEntry"]
    # Set to `True` if this page, or a descendent, is the current page.
    active: bool
    # Set to `True` if this page is the current page.
    current: bool
    # Set to `True` if this entry does not refer to a unique page but is merely
    # a TOC caption.
    caption_only: bool

    def __init__(self, title_text: str, **kwargs):
        self.__dict__.update(kwargs)
        self.title = f'<span class="md-ellipsis">{_insert_wbr(title_text)}</span>'
        if not self.aria_label:
            self.aria_label = title_text

    def __repr__(self):
        return repr(self.__dict__)


class _TocVisitor(docutils.nodes.NodeVisitor):
    """NodeVisitor used by `_get_mkdocs_toc`."""

    def __init__(
        self, document: docutils.nodes.document, builder: sphinx.builders.Builder
    ):
        super().__init__(document)
        self._prev_caption: Optional[docutils.nodes.Element] = None
        self._rendered_title_text: Optional[str] = None
        self._url: Optional[str] = None
        self._builder = builder
        # Indicates if this node or one of its descendents is the current page.
        self._active = False
        # List of direct children.
        self._children: List[MkdocsNavEntry] = []

    def _render(self, node: Union[docutils.nodes.Node, List[docutils.nodes.Node]]):
        """Returns the HTML representation of `node`."""
        if not isinstance(node, list):
            node = [node]
        return "".join(self._builder.render_partial(x)["fragment"] for x in node)

    def _render_title(
        self, node: Union[docutils.nodes.Node, List[docutils.nodes.Node]]
    ):
        """Returns the text representation of `node`."""
        if not isinstance(node, list):
            node = [node]
        return str(markupsafe.Markup.escape("".join(x.astext() for x in node)))

    def visit_reference(self, node: docutils.nodes.reference):
        self._rendered_title_text = self._render_title(node.children)
        self._url = node.get("refuri")
        raise docutils.nodes.SkipChildren

    def visit_compact_paragraph(self, node: docutils.nodes.Element):
        pass

    def visit_toctree(self, node: docutils.nodes.Node):
        raise docutils.nodes.SkipChildren

    def visit_paragraph(self, node: docutils.nodes.Node):
        pass

    # In sphinx < 3.5.4, TOC captions are represented using a caption node.
    def visit_caption(self, node: docutils.nodes.caption):
        self._prev_caption = node
        raise docutils.nodes.SkipChildren

    # In sphinx >= 3.5.4, TOC captions are represented using a title node.
    def visit_title(self, node: docutils.nodes.title):
        self._prev_caption = node
        raise docutils.nodes.SkipChildren

    def visit_bullet_list(self, node: docutils.nodes.bullet_list):
        if self._prev_caption is not None and self._prev_caption.parent is node.parent:
            # Insert as sub-entry of the previous caption.
            title_text = self._render_title(self._prev_caption.children)
            self._prev_caption = None
            child_visitor = _TocVisitor(self.document, self._builder)
            if node.get("iscurrent", False):
                child_visitor._active = True
            node.walk(child_visitor)
            url = None
            children = child_visitor._children
            if children:
                url = children[0].url
            self._children.append(
                MkdocsNavEntry(
                    title_text=title_text,
                    url=url,
                    children=children,
                    active=child_visitor._active,
                    current=False,
                    caption_only=True,
                )
            )
            raise docutils.nodes.SkipChildren
        # Otherwise, just process the each list_item as direct children.

    def get_result(self) -> MkdocsNavEntry:
        return MkdocsNavEntry(
            title_text=self._rendered_title_text,
            url=self._url,
            children=self._children,
            active=self._active,
            current=self._active and self._url == "",
            caption_only=False,
        )

    def visit_list_item(self, node: docutils.nodes.list_item):
        # Child node.  Collect its url, title, and any children using a separate
        # `_TocVisitor`.
        child_visitor = _TocVisitor(self.document, self._builder)
        if node.get("iscurrent", False):
            child_visitor._active = True
        for child in node.children:
            child.walk(child_visitor)
        child_result = child_visitor.get_result()
        self._children.append(child_result)
        raise docutils.nodes.SkipChildren


def _get_mkdocs_toc(
    toc_node: docutils.nodes.Node, builder: sphinx.builders.Builder
) -> List[MkdocsNavEntry]:
    """Converts a docutils toc node into a mkdocs-format JSON toc."""
    visitor = _TocVisitor(sphinx.util.docutils.new_document(""), builder)

    # toc_node can be None for projects with no toctree or 1 rst-file only.
    if toc_node is not None:
        toc_node.walk(visitor)
    return visitor._children


class _NavContextObject(list):
    homepage: dict


def _traverse_mkdocs_toc(toc: List[MkdocsNavEntry]) -> Iterator[MkdocsNavEntry]:
    for entry in toc:
        yield entry
        yield from _traverse_mkdocs_toc(entry.children)


def _relative_uri_to_root_relative_and_anchor(
    builder: sphinx.builders.html.StandaloneHTMLBuilder,
    base_pagename: str,
    relative_uri: str,
) -> Optional[Tuple[str, str]]:
    """Converts a relative URI to a root-relative uri and anchor."""
    uri = urllib.parse.urlparse(
        urllib.parse.urljoin(builder.get_target_uri(base_pagename), relative_uri)
    )
    if uri.netloc:
        return None
    return (uri.path, uri.fragment)


class DomainAnchorEntry(NamedTuple):
    domain_name: str
    name: str
    display_name: str
    objtype: str
    priority: int


class ObjectIconInfo(NamedTuple):
    icon_class: str
    icon_text: str


OBJECT_ICON_INFO: Dict[Tuple[str, str], ObjectIconInfo] = {
    ("std", "envvar"): ObjectIconInfo(icon_class="alias", icon_text="$"),
    ("js", "module"): ObjectIconInfo(icon_class="data", icon_text="r"),
    ("js", "function"): ObjectIconInfo(icon_class="procedure", icon_text="M"),
    ("js", "method"): ObjectIconInfo(icon_class="procedure", icon_text="M"),
    ("js", "class"): ObjectIconInfo(icon_class="data", icon_text="C"),
    ("js", "data"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("js", "attribute"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("json", "schema"): ObjectIconInfo(icon_class="data", icon_text="J"),
    ("json", "subschema"): ObjectIconInfo(icon_class="sub-data", icon_text="j"),
    ("py", "class"): ObjectIconInfo(icon_class="data", icon_text="C"),
    ("py", "function"): ObjectIconInfo(icon_class="procedure", icon_text="F"),
    ("py", "method"): ObjectIconInfo(icon_class="procedure", icon_text="M"),
    ("py", "classmethod"): ObjectIconInfo(icon_class="procedure", icon_text="M"),
    ("py", "staticmethod"): ObjectIconInfo(icon_class="procedure", icon_text="M"),
    ("py", "property"): ObjectIconInfo(icon_class="alias", icon_text="P"),
    ("py", "attribute"): ObjectIconInfo(icon_class="alias", icon_text="A"),
    ("py", "data"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("py", "parameter"): ObjectIconInfo(icon_class="sub-data", icon_text="p"),
    ("c", "member"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("c", "var"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("c", "function"): ObjectIconInfo(icon_class="procedure", icon_text="F"),
    ("c", "macro"): ObjectIconInfo(icon_class="alias", icon_text="D"),
    ("c", "union"): ObjectIconInfo(icon_class="data", icon_text="U"),
    ("c", "struct"): ObjectIconInfo(icon_class="data", icon_text="S"),
    ("c", "enum"): ObjectIconInfo(icon_class="data", icon_text="E"),
    ("c", "enumerator"): ObjectIconInfo(icon_class="data", icon_text="e"),
    ("c", "type"): ObjectIconInfo(icon_class="alias", icon_text="T"),
    ("cpp", "class"): ObjectIconInfo(icon_class="data", icon_text="C"),
    ("cpp", "struct"): ObjectIconInfo(icon_class="data", icon_text="S"),
    ("cpp", "enum"): ObjectIconInfo(icon_class="data", icon_text="E"),
    ("cpp", "enum-class"): ObjectIconInfo(icon_class="data", icon_text="E"),
    ("cpp", "enum-struct"): ObjectIconInfo(icon_class="data", icon_text="E"),
    ("cpp", "enumerator"): ObjectIconInfo(icon_class="data", icon_text="e"),
    ("cpp", "union"): ObjectIconInfo(icon_class="data", icon_text="U"),
    ("cpp", "concept"): ObjectIconInfo(icon_class="data", icon_text="t"),
    ("cpp", "function"): ObjectIconInfo(icon_class="procedure", icon_text="F"),
    ("cpp", "alias"): ObjectIconInfo(icon_class="procedure", icon_text="F"),
    ("cpp", "member"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("cpp", "var"): ObjectIconInfo(icon_class="alias", icon_text="V"),
    ("cpp", "type"): ObjectIconInfo(icon_class="alias", icon_text="T"),
    ("cpp", "namespace"): ObjectIconInfo(icon_class="alias", icon_text="N"),
}


def _make_domain_anchor_map(
    env: sphinx.environment.BuildEnvironment,
) -> Dict[Tuple[str, str], DomainAnchorEntry]:
    builder = env.app.builder
    docname_to_url = {
        docname: builder.get_target_uri(docname) for docname in env.found_docs
    }
    m: Dict[Tuple[str, str], DomainAnchorEntry] = {}
    for domain_name, domain in env.domains.items():
        for (
            name,
            dispname,
            objtype,
            docname,
            anchor,
            priority,
        ) in domain.get_objects():
            if (domain_name, objtype) not in OBJECT_ICON_INFO:
                continue
            key = (docname_to_url[docname], anchor)
            m[key] = DomainAnchorEntry(domain_name, name, dispname, objtype, priority)
    return m


def _get_domain_anchor_map(
    app: sphinx.application.Sphinx,
) -> Dict[Tuple[str, str], DomainAnchorEntry]:
    key = "sphinx_immaterial_domain_anchor_map"
    m = app.env.temp_data.get(key)
    if m is None:
        m = _make_domain_anchor_map(app.env)
        app.env.temp_data[key] = m
    return m


def _add_domain_info_to_toc(
    app: sphinx.application.Sphinx, toc: List[MkdocsNavEntry], pagename: str
) -> None:
    m = _get_domain_anchor_map(app)
    for entry in _traverse_mkdocs_toc(toc):
        if entry.caption_only or entry.url is None:
            continue
        refinfo = _relative_uri_to_root_relative_and_anchor(
            app.builder, pagename, entry.url
        )
        if refinfo is None:
            continue
        objinfo = m.get(refinfo)
        if objinfo is None:
            continue
        domain = app.env.domains[objinfo.domain_name]
        get_object_synopsis = getattr(domain, "get_object_synopsis", None)
        label = domain.get_type_name(domain.object_types[objinfo.objtype])
        tooltip = f"{objinfo.name} ({label})"
        if get_object_synopsis is not None:
            synopsis = get_object_synopsis(objinfo.objtype, objinfo.name)
            if synopsis:
                synopsis = synopsis.strip()
                if synopsis:
                    tooltip += f" — {synopsis}"
        icon_info = OBJECT_ICON_INFO.get((objinfo.domain_name, objinfo.objtype))
        title_prefix = ""
        if icon_info is not None:
            title_prefix = (
                f'<span aria-label="{label}" '
                f'class="objinfo-icon objinfo-icon__{icon_info.icon_class}" '
                f'title="{label}">{icon_info.icon_text}</span>'
            )
        span_prefix = "<span "
        assert entry.title.startswith(span_prefix)
        entry.title = (
            title_prefix
            + f'<span title="{markupsafe.Markup.escape(tooltip)}" '
            + entry.title[len(span_prefix) :]
        )


def _get_current_page_in_toc(toc: List[MkdocsNavEntry]) -> Optional[MkdocsNavEntry]:
    for entry in toc:
        if not entry.active:
            continue
        if entry.current:
            return entry
        return _get_current_page_in_toc(entry.children)
    return None


def _collapse_children_not_on_same_page(entry: MkdocsNavEntry) -> MkdocsNavEntry:
    entry = copy.copy(entry)
    if not entry.active:
        entry.children = []
    else:
        entry.children = [
            _collapse_children_not_on_same_page(child) for child in entry.children
        ]
    return entry


def _get_mkdocs_tocs(
    app: sphinx.application.Sphinx, pagename: str, duplicate_local_toc: bool
) -> Tuple[List[MkdocsNavEntry], List[MkdocsNavEntry]]:
    theme_options = app.config["html_theme_options"]
    global_toc_node = sphinx.environment.adapters.toctree.TocTree(
        app.env
    ).get_toctree_for(
        pagename,
        app.builder,
        collapse=theme_options.get("globaltoc_collapse", False),
        maxdepth=-1,
        titles_only=False,
    )
    global_toc = _get_mkdocs_toc(global_toc_node, app.builder)
    local_toc = []
    if pagename != app.env.config.master_doc:
        # Extract entry from `global_toc` corresponding to the current page.
        current_page_toc_entry = _get_current_page_in_toc(global_toc)
        if current_page_toc_entry:
            local_toc = [_collapse_children_not_on_same_page(current_page_toc_entry)]
            if not duplicate_local_toc:
                current_page_toc_entry.children = []

    else:
        # Every page is a child of the root page.  We still want a full TOC
        # tree, though.
        local_toc_node = sphinx.environment.adapters.toctree.TocTree(
            app.env
        ).get_toc_for(
            pagename,
            app.builder,
        )
        local_toc = _get_mkdocs_toc(local_toc_node, app.builder)

    _add_domain_info_to_toc(app, global_toc, pagename)
    _add_domain_info_to_toc(app, local_toc, pagename)

    if len(local_toc) == 1 and len(local_toc[0].children) == 0:
        local_toc = []

    return global_toc, local_toc


def _html_page_context(
    app: sphinx.application.Sphinx,
    pagename: str,
    templatename: str,
    context: dict,
    doctree: docutils.nodes.Node,
) -> None:
    theme_options = app.config["html_theme_options"]  # type: dict
    page_title = markupsafe.Markup.escape(
        markupsafe.Markup(context.get("title")).striptags()
    )
    meta = context.get("meta", {})
    global_toc, local_toc = _get_mkdocs_tocs(
        app,
        pagename,
        duplicate_local_toc=bool(
            meta and isinstance(meta.get("duplicate-local-toc"), str)
        ),
    )
    context.update(nav=_NavContextObject(global_toc))
    context["nav"].homepage = dict(
        url=context["pathto"](context["master_doc"]),
    )

    toc_title = theme_options.get("toc_title")
    if toc_title:
        toc_title = str(markupsafe.Markup.escape(toc_title))
    elif (
        theme_options.get("toc_title_is_page_title")
        and local_toc
        and len(local_toc) == 1
    ):
        # Use single top-level heading as table of contents heading.
        toc_title = local_toc[0].title

    context.update(
        config={
            "mdx_configs": {
                "toc": {"title": toc_title},
            },
        }
    )

    if len(local_toc) == 1:
        # If there is a single top-level heading, it is treated as the page
        # heading, and it would be redundant to also include it as an entry in
        # the local toc.
        local_toc = local_toc[0].children

    # Add other context values in mkdocs/mkdocs-material format.
    page = dict(
        title=page_title,
        is_homepage=(pagename == context["master_doc"]),
        toc=local_toc,
        meta={"hide": [], "revision_date": context.get("last_updated")},
        content=context.get("body"),
    )
    if meta:
        if meta.get("tocdepth") == 0 or "hide-toc" in meta.keys():
            page["meta"]["hide"].append("toc")
        if "hide-navigation" in meta.keys():
            page["meta"]["hide"].append("navigation")
    if context.get("next"):
        page["next_page"] = {
            "title": markupsafe.Markup.escape(
                markupsafe.Markup(context["next"]["title"]).striptags()
            ),
            "url": context["next"]["link"],
        }
    if context.get("prev"):
        page["previous_page"] = {
            "title": markupsafe.Markup.escape(
                markupsafe.Markup(context["prev"]["title"]).striptags()
            ),
            "url": context["prev"]["link"],
        }
    repo_url = theme_options.get("repo_url")  # type: str
    edit_uri = theme_options.get("edit_uri")  # type: str
    if repo_url and edit_uri and not READTHEDOCS:
        page["edit_url"] = "/".join(
            [
                repo_url.rstrip("/"),
                edit_uri.strip("/"),
                app.builder.env.doc2path(pagename, False),
            ]
        )
    context.update(
        page=page,
    )


def setup(app: sphinx.application.Sphinx):
    app.connect("html-page-context", _html_page_context)
    return {
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
