import builtins
import json
import logging
import operator
import os
import random
import shutil
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from flatlatex import converter
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pygments.formatters import HtmlFormatter
from quart import redirect
from quart_trio import QuartTrio
from rich.logging import RichHandler
from there import print

from . import config as default_config
from .config import ingest_dir
from .crosslink import IngestedBlobs, RefInfo, find_all_refs, load_one
from .graphstore import GraphStore, Key
from .stores import Store
from .take2 import RefInfo
from .utils import progress

FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("papyri")

CSS_DATA = HtmlFormatter(style="pastie").get_style_defs(".highlight")


def url(info, prefix="/p/"):
    assert isinstance(info, RefInfo)
    assert info.kind in ("module", "api", "examples", "assets", "?"), info.kind
    # assume same package/version for now.
    if info.module is None:
        assert info.version is None
        return info.path
    if info.kind == "examples":
        return f"{prefix}{info.module}/{info.version}/examples/{info.path}"
    else:
        return f"{prefix}{info.module}/{info.version}/api/{info.path}"


def unreachable(*obj):
    return str(obj)
    assert False, f"Unreachable: {obj=}"


class CleanLoader(FileSystemLoader):
    """
    A loader for ascii/ansi that remove all leading spaces and pipes  until the last pipe.
    """

    def get_source(self, *args, **kwargs):
        (source, filename, uptodate) = super().get_source(*args, **kwargs)
        return until_ruler(source), filename, uptodate


def until_ruler(doc):
    """
    Utilities to clean jinja template;

    Remove all ``|`` and `` `` until the last leading ``|``

    """
    lines = doc.split("\n")
    new = []
    for l in lines:

        while len(l.lstrip()) >= 1 and l.lstrip()[0] == "|":
            l = l.lstrip()[1:]
        new.append(l)
    return "\n".join(new)


async def examples(module, store, version, subpath, ext="", sidebar=None):
    assert sidebar is not None
    env = Environment(
        loader=FileSystemLoader(os.path.dirname(__file__)),
        autoescape=select_autoescape(["html", "tpl.j2"]),
        undefined=StrictUndefined,
    )
    env.globals["len"] = len
    env.globals["url"] = url
    env.globals["unreachable"] = unreachable

    pap_files = store.glob("*/*/papyri.json")
    parts = {module: []}
    for pp in pap_files:
        mod, ver = pp.path.parts[-3:-1]
        parts[module].append((RefInfo(mod, ver, "api", mod), mod) + ext)

    efile = store / module / version / "examples" / subpath
    from .take2 import Section

    ex = Section.from_json(json.loads(await efile.read_text()))

    class Doc:
        pass

    doc = Doc()
    doc.logo = None

    return env.get_template("examples.tpl.j2").render(
        pygment_css=CSS_DATA,
        module=module,
        parts=parts,
        ext=ext,
        version=version,
        parts_links=defaultdict(lambda: ""),
        doc=doc,
        ex=ex,
        sidebar=sidebar,
    )


# here we compute the siblings at each level; as well as one level down
# this is far from efficient and a hack, but it helps with navigation.
# I'm pretty sure we load the full library while we could
# load only the current module id not less, and that this could
# be done at ingest time or cached.
# So basically in the breadcrumps
# IPython.lib.display.+
#  - IPython will be siblings with numpy; scipy, dask, ....
#  - lib (or "IPython.lib"), with "core", "display", "terminal"...
#  etc.
#  - + are deeper children's
#
# This is also likely a bit wrong; as I'm sure we want to only show
# submodules or sibling modules and not attribute/instance of current class,
# though that would need loading the files and looking at the types of
# things. likely want to store that in a tree somewhere But I thing this is
# doable after purely as frontend thing.


def compute_siblings_II(ref, family: set):
    """ """
    from collections import defaultdict

    module_versions = defaultdict(lambda: set())
    for f in family:
        module_versions[f.module].add(f.version)

    module_versions_max = {k: max(v) for k, v in module_versions.items()}

    family = {f for f in family if f.version == module_versions_max[f.module]}

    parts = ref.split(".") + ["+"]
    siblings = OrderedDict()
    cpath = ""

    # TODO: move this at ingestion time for all the non-top-level.
    for i, part in enumerate(parts):
        candidates = [c for c in family if c.path.startswith(cpath) and "." in c.path]
        # trm down to the right length
        candidates = [
            RefInfo(c.module, c.version, "api", ".".join(c.path.split(".")[: i + 1]))
            for c in candidates
        ]
        sib = list(sorted(set(candidates), key=operator.attrgetter("path")))

        siblings[part] = [(c, c.path.split(".")[-1]) for c in sib]
        cpath += part + "."
    if not siblings["+"]:
        del siblings["+"]
    return siblings


def make_tree(names):

    rd = lambda: defaultdict(rd)
    tree = defaultdict(rd)

    for n in names:
        parts = n.split(".")
        branch = tree
        for p in parts:
            branch = branch[p]
    return tree


def cs2(ref, tree, ref_map):
    """
    IIRC this is quite similar to compute_siblings(_II),
    but more efficient as we know we are going to compute
    all the siblings and not just the local one when rendering a single page.

    """
    parts = ref.split(".") + ["+"]
    siblings = OrderedDict()
    cpath = ""
    branch = tree

    def GET(ref_map, key, cpath):
        if key in ref_map:
            return ref_map[key]
        else:
            # this is a tiny bit weird; and will need a workaround at some
            # point.
            # this happends when one object in the hierarchy has not docs
            # (typically a class which is documented only in __init__)
            # or when a object does not match its qualified name (typically in
            # numpy, and __init__ with:
            #  from .foo import foo
            # leading to xxx.foo meaning being context dependant.
            # for now we return a dummy object.
            # print(f"{key=} seem to be a sibling with")
            # print(f"     {cpath=}, but was not ")
            # print(f"     found when trying to compute navigation for")
            # print(f"     {ref=}")
            # print(f"     Here are possible causes:")
            # print(f"        - it is a class and only __init__ has docstrings")
            # print(f"        - it is stored with the wrong qualname          ")
            # print(f"                                             ")

            return RefInfo("?", "?", "?", key)

    for p in parts:
        res = list(sorted((f"{cpath}{k}", k) for k in branch.keys() if k != "+"))
        if res:
            siblings[p] = [
                ##(ref_map.get(c, RINFO("?", "?", "?", c)), c.split(".")[-1])
                (GET(ref_map, c, cpath), c.split(".")[-1])
                for c, k in res
            ]
        else:
            break

        branch = branch[p]
        cpath += p + "."
    return siblings


def compute_graph(gs, blob, key):
    # nodes_names = [b.path for b in blob.backrefs + blob.refs] + [key[3]]
    # nodes_names = [n for n in nodes_names if n.startswith('numpy')]
    weights = {}

    all_nodes = [tuple(x) for x in blob.backrefs + blob.refs]

    raw_edges = []
    for k in blob.backrefs + blob.refs:
        name = tuple(k)[3]
        neighbors_refs = gs.get_backref(tuple(k))
        weights[name] = len(neighbors_refs)
        orig = [x[3] for x in neighbors_refs]
        all_nodes.extend([tuple(x) for x in neighbors_refs])
        for o in orig:
            raw_edges.append((k.path, o))

    data = {"nodes": [], "links": []}

    if len(weights) > 50:
        for thresh in sorted(set(weights.values())):
            log.info("%s items ; remove items %s or lower", len(weights), thresh)
            weights = {k: v for k, v in weights.items() if v > thresh}
            log.info("down to %s items", len(weights))
            if len(weights) < 50:
                break

    all_nodes = set(all_nodes)
    nums_ = set()
    edges = list(raw_edges)
    nodes = list(set(weights.keys()))
    for a, b in edges:
        if (a not in nodes) or (b not in nodes):
            continue
        nums_.add(a)
        nums_.add(b)
    nums = {x: i for i, x in enumerate(nodes, start=1)}

    for i, (from_, to) in enumerate(edges):
        if from_ == to:
            continue
        if from_ not in nodes:
            continue
        if to not in nodes:
            continue
        if key[3] in (to, from_):
            continue
        data["links"].append({"source": nums[from_], "target": nums[to], "id": i})

    for node in nodes:
        diam = 8
        if node == key[3]:
            continue
            diam = 18
        elif node in weights:
            import math

            diam = 8 + math.sqrt(weights[node])

        candidates = [n for n in all_nodes if n[3] == node and "??" not in n]
        if not candidates:
            uu = None
        else:
            # TODO : be smarter when we have multiple versions. Here we try to pick the latest one.
            latest_version = list(sorted(candidates))[-1]
            uu = url(RefInfo(*latest_version))

        data["nodes"].append(
            {
                "id": nums[node],
                "val": diam,
                "label": node,
                "mod": ".".join(node.split(".")[0:1]),
                "url": uu,
            }
        )
    return data


async def _route_data(gstore, ref, version, known_refs):
    print("!!", ref)
    root = ref.split("/")[0].split(".")[0]
    key = Key(root, version, "module", ref)
    gbytes = gstore.get(key).decode()
    x_, y_ = find_all_refs(gstore)
    doc_blob = load_one(gbytes, b"[]", known_refs=known_refs, strict=True)
    return x_, y_, doc_blob


class HtmlRenderer:
    def __init__(self, store, *, sidebar, old_store):
        self.store = store
        self.old_store = old_store
        prefix = "/p/"
        self.env = Environment(
            loader=FileSystemLoader(os.path.dirname(__file__)),
            autoescape=select_autoescape(["html", "tpl.j2"]),
            undefined=StrictUndefined,
        )
        self.env.globals["len"] = len
        self.env.globals["url"] = lambda x: url(x, prefix)
        self.env.globals["unreachable"] = unreachable
        self.env.globals["prefix"] = prefix
        self.env.globals["sidebar"] = sidebar
        self.sidebar = sidebar

    async def gallery(self, module, version, ext=""):

        figmap = defaultdict(lambda: [])
        res = self.store.glob((module, version, "assets", None))
        backrefs = set()
        for key in res:
            brs = {tuple(x) for x in self.store.get_backref(key)}
            backrefs = backrefs.union(brs)

        for key in backrefs:
            data = json.loads(self.store.get(Key(*key)).decode())
            data["backrefs"] = []

            i = IngestedBlobs.from_json(data)
            i.process(frozenset(), {})

            for k in [
                u.value for u in i.example_section_data if u.__class__.__name__ == "Fig"
            ]:
                module, v, kind, _path = key
                # module, filename, link
                impath = f"/p/{module}/{v}/img/{k}"
                link = f"/p/{module}/{v}/api/{_path}"
                # figmap.append((impath, link, name)
                figmap[module].append((impath, link, _path))

        for target_path in self.old_store.glob(f"{module}/{version}/examples/*"):
            data = json.loads(await target_path.read_text())
            from .take2 import Section

            s = Section.from_json(data)

            for k in [u.value for u in s.children if u.__class__.__name__ == "Fig"]:
                module, v, _, _path = target_path.path.parts[-4:]

                # module, filename, link
                impath = f"/p/{module}/{v}/img/{k}"
                link = f"/p/{module}/{v}/examples/{target_path.name}"
                name = target_path.name
                # figmap.append((impath, link, name)
                figmap[module].append((impath, link, name))

        env = Environment(
            loader=FileSystemLoader(os.path.dirname(__file__)),
            autoescape=select_autoescape(["html", "tpl.j2"]),
            undefined=StrictUndefined,
        )
        env.globals["len"] = len
        env.globals["url"] = url
        env.globals["sidebar"] = self.sidebar

        class D:
            pass

        doc = D()
        doc.logo = "logo.png"

        pap_files = self.old_store.glob("*/*/papyri.json")
        parts = {module: []}
        for pp in pap_files:
            mod, ver = pp.path.parts[-3:-1]
            parts[module].append((RefInfo(mod, ver, "api", mod), mod))

        return env.get_template("gallery.tpl.j2").render(
            figmap=figmap,
            pygment_css="",
            module=module,
            parts=parts,
            ext=ext,
            version=version,
            parts_links=defaultdict(lambda: ""),
            doc=doc,
        )

    async def _serve_narrative(self, package: str, version: str, ref: str):
        """
        Serve the narrative part of the documentation for given package
        """
        # return "Not Implemented"
        bytes = self.store.get(Key(package, version, "docs", ref))
        doc_blob = load_one(bytes, b"[]", known_refs=frozenset(), strict=True)
        print(doc_blob)
        # return "OK"

        template = self.env.get_template("html.tpl.j2")

        # ...
        return render_one(
            template=template,
            doc=doc_blob,
            qa="numpy",
            ext="",
            parts={"numpy": []},
            parts_links={},
            backrefs=[],
            pygment_css=CSS_DATA,
            graph="{}",
            sidebar=self.sidebar,
        )

    async def _route(
        self,
        ref,
        store,
        version=None,
        gstore=None,
    ):
        assert not ref.endswith(".html")
        assert version is not None
        assert ref != ""

        env = self.env

        template = env.get_template("html.tpl.j2")
        root = ref.split(".")[0]

        known_refs, ref_map = find_all_refs(store)

        # technically incorrect we don't load backrefs
        x_, y_, doc_blob = await _route_data(self.store, ref, version, known_refs)
        assert x_ == known_refs
        assert y_ == ref_map
        assert version is not None

        siblings = compute_siblings_II(ref, known_refs)

        # End computing siblings.
        if version is not None:
            file_ = store / root / version / "module" / f"{ref}"
        else:
            assert False
            # files = list((store / root).glob(f"*/module/{ge(ref)}"))
        if await file_.exists():
            # The reference we are trying to view exists;
            # we will now just render it.
            # bytes_ = await file_.read_text()
            assert root is not None
            # assert version is not None
            brpath = store / root / version / "module" / f"{ref}.br"
            print(brpath)
            if await brpath.exists():
                br = await brpath.read_text()
                # TODO: update to new way of getting backrefs.
                br = None
            else:
                br = None

            data = compute_graph(self.store, doc_blob, (root, version, "module", ref))
            json_str = json.dumps(data)
            parts_links = {}
            acc = ""
            for k in siblings.keys():
                acc += k
                parts_links[k] = acc
                acc += "."

            return render_one(
                template=template,
                doc=doc_blob,
                qa=ref,
                ext="",
                parts=siblings,
                parts_links=parts_links,
                backrefs=doc_blob.backrefs,
                pygment_css=CSS_DATA,
                graph=json_str,
                sidebar=self.sidebar,
            )
        else:
            # The reference we are trying to render does not exists
            # just try to have a nice  error page and try to find local reference and
            # use the phantom file to list the backreferences to this.
            # it migt be a page, or a module we do not have documentation about.
            r = ref.split(".")[0]
            this_module_known_refs = [
                str(s.name)
                for s in store.glob(f"{r}/*/module/{ref}")
                if not s.name.endswith(".br")
            ]
            x2 = [x.path for x in self.store.glob((r, None, "module", ref))]
            assert set(x2) == set(this_module_known_refs), (
                set(x2) - set(this_module_known_refs),
                (set(this_module_known_refs) - set(x2)),
            )
            brpath = store / "__phantom__" / f"{ref}.json"
            if await brpath.exists():
                br = json.loads(await brpath.read_text())
            else:
                br = []

            # compute a tree from all the references we have to have a nice browsing
            # interfaces.
            tree = {}
            for f in this_module_known_refs:
                sub = tree
                parts = f.split(".")[len(ref.split(".")) :]
                for part in parts:
                    if part not in sub:
                        sub[part] = {}
                    sub = sub[part]

                sub["__link__"] = f

            error = env.get_template("404.tpl.j2")
            return error.render(backrefs=list(set(br)), tree=tree, ref=ref, module=root)


async def img(package, version, subpath=None) -> Optional[bytes]:
    file = ingest_dir / package / version / "assets" / subpath
    if file.exists():
        return file.read_bytes()
    return None


def static(name):
    here = Path(os.path.dirname(__file__))

    def f():
        return (here / name).read_bytes()

    return f


def logo() -> bytes:

    path = os.path.abspath(__file__)
    dir_path = Path(os.path.dirname(path))
    with open((dir_path / "papyri-logo.png"), "rb") as f:
        return f.read()


def serve(*, sidebar: bool):

    app = QuartTrio(__name__)

    store = Store(str(ingest_dir))
    gstore = GraphStore(ingest_dir)
    html_renderer = HtmlRenderer(gstore, sidebar=sidebar, old_store=store)

    async def full(package, version, ref):
        return await html_renderer._route(ref, store, version)

    async def full_gallery(module, version):
        return await html_renderer.gallery(module, version)

    async def g(module):
        return await html_renderer.gallery(module)

    async def gr():
        return await html_renderer.gallery("*")

    async def index():
        import papyri

        v = str(papyri.__version__)
        return redirect(f"/p/papyri/{v}/api/papyri")

    async def ex(module, version, subpath):
        return await examples(
            module=module,
            store=store,
            version=version,
            subpath=subpath,
            sidebar=sidebar,
        )

    app.route("/logo.png")(logo)
    app.route("/favicon.ico")(static("favicon.ico"))
    # sub here is likely incorrect
    app.route("/p/<package>/<version>/img/<path:subpath>")(img)
    app.route("/p/<module>/<version>/examples/<path:subpath>")(ex)
    app.route("/p/<module>/<version>/gallery")(full_gallery)
    app.route("/p/<package>/<version>/docs/<ref>")(html_renderer._serve_narrative)
    app.route("/p/<package>/<version>/api/<ref>")(full)
    app.route("/gallery/")(gr)
    app.route("/gallery/<module>")(g)
    app.route("/")(index)
    port = int(os.environ.get("PORT", 1234))
    print("Seen config port ", port)
    prod = os.environ.get("PROD", None)
    if prod:
        app.run(port=port, host="0.0.0.0")
    else:
        app.run(port=port)


def render_one(
    template,
    doc: IngestedBlobs,
    qa,
    ext,
    *,
    backrefs,
    pygment_css=None,
    parts=(),
    parts_links=(),
    graph="{}",
    sidebar,
):
    """
    Return the rendering of one document

    Parameters
    ----------
    template
        a Jinja@ template object used to render.
    doc : DocBlob
        a Doc object with the informations for current obj
    qa : str
        fully qualified name for current object
    ext : str
        file extension for url  – should likely be removed and be set on the template
        I think that might be passed down to resolve maybe ?
    backrefs : list of str
        backreferences of document pointing to this.
    parts : Dict[str, list[(str, str)]
        used for navigation and for parts of the breakcrumbs to have navigation to siblings.
        This is not directly related to current object.

    """
    # TODO : move this to ingest likely.
    # Here if we have too many references we group them on where they come from.
    if len(backrefs) > 30:

        b2 = defaultdict(lambda: [])
        for ref in backrefs:
            assert isinstance(ref, RefInfo)
            if "." in ref.path:
                mod, _ = ref.path.split(".", maxsplit=1)
            else:
                mod = ref.path
            b2[mod].append(ref)
        backrefs = (None, b2)
    else:
        backrefs = (backrefs, None)

    try:
        return template.render(
            doc=doc,
            qa=qa,
            version=doc.version,
            module=qa.split(".")[0],
            backrefs=backrefs,
            ext=ext,
            parts=parts,
            parts_links=parts_links,
            pygment_css=pygment_css,
            graph=graph,
            sidebar=sidebar,
        )
    except Exception as e:
        raise ValueError("qa=", qa) from e


@lru_cache
def _ascii_env():
    env = Environment(
        loader=CleanLoader(os.path.dirname(__file__)),
        lstrip_blocks=True,
        trim_blocks=True,
        undefined=StrictUndefined,
    )
    env.globals["len"] = len
    env.globals["unreachable"] = unreachable
    try:

        c = converter()

        def math(s):
            assert isinstance(s, list)
            for x in s:
                assert isinstance(x, str)
            res = [c.convert(_) for _ in s]
            print(res)
            return res

        env.globals["math"] = math
    except ImportError:

        def math(s):
            return s + "($pip install flatlatex for unicode math)"

        env.globals["math"] = math

    template = env.get_template("ascii.tpl.j2")
    return env, template


async def _ascii_render(key, store, known_refs=None, template=None):
    if store is None:
        store = GraphStore(ingest_dir)
    assert isinstance(store, GraphStore)
    ref = key.path

    # keys = store.glob((root, None))
    # version = keys[0][-1]

    env, template = _ascii_env()
    bytes_ = store.get(key).decode()

    # TODO:
    # brpath = store / root / rsion / "module" / f"{ref}.br"
    # if await brpath.exists():
    #    br = await brpath.read_text()
    #    br = None
    # else:
    #    br = None
    br = None

    doc_blob = load_one(bytes_, br, strict=True)

    # exercise the reprs
    assert str(doc_blob)

    data = compute_graph(store, doc_blob, key)
    json_str = json.dumps(data)
    return render_one(
        template=template,
        doc=doc_blob,
        qa=ref,
        ext="",
        backrefs=doc_blob.backrefs,
        pygment_css=None,
        graph=json_str,
        sidebar=False,  # no effects
    )


async def ascii_render(name, store=None):
    gstore = GraphStore(ingest_dir, {})
    key = next(iter(gstore.glob((None, None, "module", "papyri.examples"))))

    builtins.print(await _ascii_render(key, store))


async def loc(document: Key, *, store: GraphStore, tree, known_refs, ref_map):
    """
    return data for rendering in the templates

    Parameters
    ----------
    document: Store
        Path the document we need to read and prepare for rendering
    store: Store

        Store into which the document is stored (abstraciton layer over local
        filesystem or a remote store like github, thoug right now only local
        file system works)
    tree:
        tree of object we know about; this will be useful to compute siblings
        for the navigation menu at the top that allow to either drill down the
        hierarchy.
    known_refs: List[RefInfo]
        list of all the reference info for targets, so that we can resolve links
        later on; this is here for now, but shoudl be moved to ingestion at some
        point.
    ref_map: ??
        helper to compute the siblings for agiven hierarchy,

    Returns
    -------
    doc_blob: IngestedBlobs
        document that will be rendered
    qa: str
        fully qualified name of the object we will render
    siblings:
        information to render the navigation dropdown at the top.
    parts_links:
        information to render breadcrumbs with links to parents.


    Notes
    -----

    Note that most of the current logic assume we only have the documentation
    for a single version of a package; when we have multiple version some of
    these heuristics break down.

    """
    assert isinstance(document, Key), type(document)
    if isinstance(document, Key):
        qa = document.path
        version = document.version
        root = document.module
        # qa = document.name[:-5]
        # version = document.path.parts[-3]
        # help to keep ascii bug free.
        # await _ascii_render(qa, store, known_refs=known_refs)
        root = qa.split(".")[0]
    elif isinstance(document, tuple):
        assert False, f"Document is {document}"  # happens in render.
        qa = document.path
        version = document.version
        root = document.module
    else:
        assert False
    try:
        if isinstance(document, tuple):
            assert isinstance(store, GraphStore)
            bytes_ = store.get(document)
        else:
            bytes_ = await document.read_text()
        if isinstance(store, Store):
            brpath = store / root / version / "module" / f"{qa}.br"
            assert await brpath.exists()
            br = await brpath.read_text()
        elif isinstance(store, GraphStore):
            gbr_data = store.get_backref(document)
            gbr_bytes = json.dumps([RefInfo(*x).to_json() for x in gbr_data]).encode()
            br = gbr_bytes
        else:
            assert False
        doc_blob: IngestedBlobs = load_one(
            bytes_, br, known_refs=known_refs, strict=True
        )

    except Exception as e:
        raise RuntimeError(f"error with {document}") from e

    siblings = cs2(qa, tree, ref_map)

    parts_links = {}
    acc = ""
    for k in siblings.keys():
        acc += k
        parts_links[k] = acc
        acc += "."
    try:
        return doc_blob, qa, siblings, parts_links
    except Exception as e:
        raise type(e)(f"Error in {qa}") from e


async def _self_render_as_index_page(
    html_dir: Optional[Path],
    gstore,
    tree,
    known_refs,
    ref_map,
    config,
    template,
    css_data,
) -> None:
    """
    Currently we do not have any logic for an index page (we should).
    So we'll just render the documentation for the papyri module itself.

    Parameters
    ----------
    html : bool
        whether we are building html docs.
    html_dir: path
        where should the index be writte
    tree:
    known_refs:
    ref_map:
    sidebar: bool
        whether to render the sidebar.
    template:
        which template to use
    css_data:


    Returns
    -------
    None



    """

    if html_dir is not None:
        assert isinstance(html_dir, Path)

    if not config.html:
        return

    import papyri

    key = Key("papyri", str(papyri.__version__), "module", "papyri")

    doc_blob, qa, siblings, parts_links = await loc(
        key,
        store=gstore,
        tree=tree,
        known_refs=known_refs,
        ref_map=ref_map,
    )
    data = render_one(
        sidebar=config.html_sidebar,
        template=template,
        doc=doc_blob,
        qa=qa,
        ext=".html",
        parts=siblings,
        parts_links=parts_links,
        backrefs=doc_blob.backrefs,
        pygment_css=css_data,
    )
    if html_dir:
        with (html_dir / "index.html").open("w") as f:
            f.write(data)


async def _write_gallery(store, gstore, config):
    """ """
    mv2 = gstore.glob((None, None))
    html_renderer = HtmlRenderer(gstore, sidebar=config.html_sidebar, old_store=store)
    for _, (module, version) in progress(
        set(mv2), description="Rendering galleries..."
    ):
        # version, module = item.path.name, item.path.parent.name
        data = await html_renderer.gallery(
            module,
            version,
            ext=".html",
        )
        if config.output_dir:
            (config.output_dir / module / version / "gallery").mkdir(
                parents=True, exist_ok=True
            )
            (
                config.output_dir / module / version / "gallery" / "index.html"
            ).write_text(data)


@dataclass
class StaticRenderingConfig:
    """Class for keeping track of an item in inventory."""

    html: bool
    html_sidebar: bool
    ascii: bool
    output_dir: Optional[Path]


async def main(ascii: bool, html, dry_run, sidebar):
    """
    This does static rendering of all the given files.

    Parameters
    ----------
    ascii: bool
        whether to render ascii files.
    html: bool
        whether to render the html
    dry_run: bool
        do not write the output.
    Sidebar:bool
        render the sidebar in html

    """

    html_dir_: Optional[Path] = default_config.html_dir
    if dry_run:
        output_dir = None
        html_dir_ = None
    else:
        assert html_dir_ is not None
        output_dir = html_dir_ / "p"
        output_dir.mkdir(exist_ok=True)
    config = StaticRenderingConfig(html, sidebar, ascii, output_dir)

    gstore = GraphStore(ingest_dir, {})
    store = Store(ingest_dir)
    gfiles = list(gstore.glob((None, None, "module", None)))

    css_data = HtmlFormatter(style="pastie").get_style_defs(".highlight")
    env = Environment(
        loader=FileSystemLoader(os.path.dirname(__file__)),
        autoescape=select_autoescape(["html", "tpl.j2"]),
        undefined=StrictUndefined,
    )
    env.globals["len"] = len
    env.globals["unreachable"] = unreachable
    env.globals["url"] = url
    template = env.get_template("html.tpl.j2")
    document: Store

    x_, y_ = find_all_refs(store)
    known_refs, ref_map = find_all_refs(gstore)
    assert x_ == known_refs
    assert y_ == ref_map
    # end

    family = frozenset(_.path for _ in known_refs)

    tree = make_tree(family)
    if html_dir_ is not None:
        log.info("going to erase %s", html_dir_)
        shutil.rmtree(html_dir_)
    else:
        log.info("no output dir, we'll try not to touch the filesystem")

    # shuffle files to detect bugs, just in case.
    random.shuffle(gfiles)
    # Gallery

    await _write_gallery(store, gstore, config)

    await _write_example_files(gstore, config)

    await _write_api_file(
        gfiles,
        gstore,
        tree,
        known_refs,
        ref_map,
        template,
        css_data,
        config,
    )

    await _self_render_as_index_page(
        html_dir_, gstore, tree, known_refs, ref_map, config, template, css_data
    )
    await copy_assets(config, gstore)


async def _write_example_files(gstore, config):
    if not config.html:
        return

    examples = list(gstore.glob((None, None, "examples", None)))
    env = Environment(
        loader=FileSystemLoader(os.path.dirname(__file__)),
        autoescape=select_autoescape(["html", "tpl.j2"]),
        undefined=StrictUndefined,
    )
    env.globals["len"] = len
    env.globals["url"] = url
    env.globals["unreachable"] = unreachable
    for _, example in progress(examples, description="Rendering Examples..."):
        module, version, _, path = example
        data = await render_single_examples(
            env,
            module,
            gstore,
            version,
            ".html",
            config.html_sidebar,
            gstore.get(example),
        )
        if config.output_dir:
            (config.output_dir / module / version / "examples").mkdir(
                parents=True, exist_ok=True
            )
            (
                config.output_dir / module / version / "examples" / f"{path}.html"
            ).write_text(data)


async def render_single_examples(env, module, gstore, version, ext, sidebar, data):
    assert sidebar is not None
    css_data = HtmlFormatter(style="pastie").get_style_defs(".highlight")

    mod_vers = gstore.glob((None, None))
    parts = {module: []}
    for mod, ver in mod_vers:
        assert isinstance(mod, str)
        assert isinstance(ver, str)
        parts[module].append((RefInfo(mod, ver, "api", mod), mod))

    from .take2 import Section

    ex = Section.from_json(json.loads(data))

    class Doc:
        pass

    doc = Doc()
    doc.logo = None

    return env.get_template("examples.tpl.j2").render(
        pygment_css=css_data,
        module=module,
        parts=parts,
        ext=ext,
        version=version,
        parts_links=defaultdict(lambda: ""),
        doc=doc,
        ex=ex,
        sidebar=sidebar,
    )


async def _write_api_file(
    gfiles,
    gstore,
    tree,
    known_refs,
    ref_map,
    template,
    css_data,
    config,
):

    for _, key in progress(gfiles, description="Rendering API..."):
        module, version = key.module, key.version
        if config.ascii:
            await _ascii_render(key, store=gstore)
        if config.html:
            doc_blob, qa, siblings, parts_links = await loc(
                key,
                store=gstore,
                tree=tree,
                known_refs=known_refs,
                ref_map=ref_map,
            )
            data = compute_graph(gstore, doc_blob, key)
            json_str = json.dumps(data)
            data = render_one(
                template=template,
                doc=doc_blob,
                qa=qa,
                ext=".html",
                parts=siblings,
                parts_links=parts_links,
                backrefs=doc_blob.backrefs,
                pygment_css=css_data,
                graph=json_str,
                sidebar=config.html_sidebar,
            )
            if config.output_dir:
                (config.output_dir / module / version / "api").mkdir(
                    parents=True, exist_ok=True
                )
                (
                    config.output_dir / module / version / "api" / f"{qa}.html"
                ).write_text(data)


async def copy_assets(config, gstore):
    """
    Copy assets from to their final destination.

    Assets are all the binary files that we don't want to change.
    """
    if config.output_dir is None:
        return

    assets_2 = gstore.glob((None, None, "assets", None))
    for _, asset in progress(assets_2, description="Copying assets"):
        b = config.output_dir / asset.module / asset.version / "img"
        b.mkdir(parents=True, exist_ok=True)
        data = gstore.get(asset)
        (b / asset.path).write_bytes(data)
