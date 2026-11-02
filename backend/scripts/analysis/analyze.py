"""Code metrics for one snapshot. Usage: analyze.py <root> <mode>
mode 'app'  : components = second-level packages under backend/app (app.api, app.db, ...)
mode 'svc'  : components = top-level Python packages found under services/*/src, packages/*/src, events
Outputs JSON on stdout."""
import ast, json, os, sys, statistics as st
from collections import defaultdict
import lizard
from radon.complexity import cc_visit
from radon.metrics import mi_visit
from radon.raw import analyze as raw_analyze

root, mode = sys.argv[1], sys.argv[2]
files = []
for d, dn, fn in os.walk(root):
    dn[:] = [x for x in dn if x not in ("__pycache__", ".venv", "node_modules")]
    for f in fn:
        if f.endswith(".py"):
            files.append(os.path.join(d, f))


def modname(path):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if mode == "app":
        i = parts.index("app")
        parts = parts[i:]
    else:
        if "src" in parts:
            parts = parts[parts.index("src") + 1:]
        else:
            parts = [p for p in parts if p != "backend"]
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def component(mod):
    p = mod.split(".")
    if mode == "app":
        return ".".join(p[:2]) if len(p) > 1 else p[0]
    return p[0]


mods = {}
for f in files:
    mods[modname(f)] = f
comps = {component(m) for m in mods}
tops = {m.split(".")[0] for m in mods}

tot = defaultdict(int)
cc_all, mi_all, lcom_all = [], [], []
classes = funcs = 0
edges = defaultdict(set)
ext_imports = defaultdict(set)
per_comp = defaultdict(lambda: defaultdict(int))
for m, f in mods.items():
    src = open(f, encoding="utf-8", errors="replace").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    r = raw_analyze(src)
    c = component(m)
    for k in ("loc", "sloc", "lloc", "comments"):
        tot[k] += getattr(r, k)
        per_comp[c][k] += getattr(r, k)
    tot["files"] += 1
    if r.sloc:
        mi_all.append(mi_visit(src, multi=True))
    for b in cc_visit(src):
        cc_all.append(b.complexity)
    li = lizard.analyze_file.analyze_source_code(f, src)
    funcs += len(li.function_list)
    # imports
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against current module
                base = m.split(".")
                is_pkg = os.path.basename(f) == "__init__.py"
                base = base if is_pkg else base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                targets = [".".join(base + ([node.module] if node.module else []))]
            elif node.module:
                targets = [node.module]
        if isinstance(node, ast.ImportFrom):
            expanded = []
            for t in targets:
                for a in node.names:
                    expanded.append(t + "." + a.name if (t + "." + a.name) in mods else t)
            targets = expanded
        for t in targets:
            top = t.split(".")[0]
            if top in tops:
                # find longest matching internal module/package
                tc = component(t)
                if tc in comps and tc != c:
                    edges[c].add(tc)
            else:
                ext_imports[c].add(top)
    # classes / LCOM4
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes += 1
            meths = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name != "__init__" and not any(
                         isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod") for d in n.decorator_list)]
            if len(meths) < 2:
                continue
            attrs, calls = {}, defaultdict(set)
            names = {n.name for n in meths}
            for n in meths:
                a = set()
                for x in ast.walk(n):
                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                        if x.attr in names:
                            calls[n.name].add(x.attr)
                        else:
                            a.add(x.attr)
                attrs[n.name] = a
            # union-find
            parent = {n: n for n in names}
            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x
            def union(a, b):
                parent[find(a)] = find(b)
            ns = list(names)
            for i, a in enumerate(ns):
                for b in ns[i + 1:]:
                    if attrs[a] & attrs[b]:
                        union(a, b)
            for a, bs in calls.items():
                for b in bs:
                    union(a, b)
            lcom_all.append(len({find(n) for n in names}))

ca = defaultdict(set)
for a, bs in edges.items():
    for b in bs:
        ca[b].add(a)
comp_rows = []
for c in sorted(comps):
    ce_, ca_ = len(edges.get(c, ())), len(ca.get(c, ()))
    comp_rows.append({"component": c, "sloc": per_comp[c]["sloc"], "Ce": ce_, "Ca": ca_,
                      "I": round(ce_ / (ca_ + ce_), 2) if ca_ + ce_ else None,
                      "depends_on": sorted(edges.get(c, ()))})
Is = [r["I"] for r in comp_rows if r["I"] is not None]
out = {
    "files": tot["files"], "loc": tot["loc"], "sloc": tot["sloc"], "lloc": tot["lloc"],
    "comment_lines": tot["comments"], "classes": classes, "functions_lizard": funcs,
    "cc_blocks": len(cc_all), "cc_mean": round(st.mean(cc_all), 2) if cc_all else None,
    "cc_max": max(cc_all) if cc_all else None, "cc_blocks_C_or_worse(>10)": sum(1 for x in cc_all if x > 10),
    "mi_mean": round(st.mean(mi_all), 1) if mi_all else None, "mi_min": round(min(mi_all), 1) if mi_all else None,
    "components": len(comps), "I_mean": round(st.mean(Is), 2) if Is else None,
    "edges_total": sum(len(v) for v in edges.values()),
    "lcom4_classes_measured": len(lcom_all), "lcom4_mean": round(st.mean(lcom_all), 2) if lcom_all else None,
    "lcom4_classes_gt1": sum(1 for x in lcom_all if x > 1),
    "component_table": comp_rows,
}
print(json.dumps(out, indent=1))
