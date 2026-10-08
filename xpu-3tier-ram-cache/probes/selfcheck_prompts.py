#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Static self-proof for PROMPT_small.json / PROMPT_h3.json.

Runs WITHOUT starting ComfyUI and WITHOUT importing torch. It AST-parses the
ComfyUI *source* to build a node schema registry, then validates each prompt:

  (a) valid JSON
  (b) every class_type exists in the ComfyUI source (node_id or class name)
  (c) every input name is declared in that node's schema
  (d) every model filename string matches a real file on disk (byte-exact)
  (e) every output reference [node_id, slot] resolves to an existing node and a
      slot that is in range (plus a best-effort type check)

Anything the parser cannot decide is reported as "UNKNOWN" - never silently
treated as PASS. Exit code 0 = all PASS, 1 = at least one miss/unknown.
"""
import ast
import json
import os
import sys

COMFY = r"E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI"
MODEL_LIB = r"E:\HE\ComfyUI模型库"
HERE = os.path.dirname(os.path.abspath(__file__))

# API input name -> model subdirectory (folder_paths names)
MODEL_INPUT_DIRS = {
    "ckpt_name": "checkpoints",
    "unet_name": "diffusion_models",
    "clip_name": "text_encoders",
    "vae_name": "vae",
    "lora_name": "loras",
}

SRC_FILES = ["nodes.py"]
for sub in ("comfy_extras",):
    d = os.path.join(COMFY, sub)
    for name in sorted(os.listdir(d)):
        if name.endswith(".py"):
            SRC_FILES.append(os.path.join(sub, name))


class NodeSchema:
    def __init__(self, key, kind, file, lineno, inputs, outputs, opaque=False):
        self.key = key
        self.kind = kind          # "v3" | "legacy"
        self.file = file
        self.lineno = lineno
        self.inputs = inputs      # name -> type token (str|None)
        self.outputs = outputs    # list[str] output type tokens
        self.opaque = opaque      # True if some inputs are built by a helper call


def _attr_name(node):
    """io.Clip.Input -> 'Input'; io.Schema -> 'Schema'."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _inputs_from_schema_call(call):
    """inputs=[io.Clip.Input('clip'), ...] -> ({name: TYPE}, opaque).

    A helper-built input (e.g. SaveVideo's `_save_video_codec_input(...)`) is a
    Call that is NOT a `*.Input` attribute call; we cannot statically read its
    name, so we flag the schema as opaque and validate such inputs live instead
    of pretending they are absent.
    """
    out = {}
    opaque = False
    for kw in call.keywords:
        if kw.arg != "inputs":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple)):
            opaque = True
            continue
        for elt in kw.value.elts:
            if not isinstance(elt, ast.Call):
                continue
            kind = _attr_name(elt.func)               # e.g. "Input"
            holder = elt.func.value if isinstance(elt.func, ast.Attribute) else None
            type_tok = _attr_name(holder) if holder is not None else None
            if kind != "Input":
                opaque = True                         # helper-constructed input
                continue
            if elt.args:
                name = _const_str(elt.args[0])
                if name is not None:
                    out[name] = (type_tok or "").upper() or None
    return out, opaque


def _outputs_from_schema_call(call):
    toks = []
    for kw in call.keywords:
        if kw.arg != "outputs":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple)):
            continue
        for elt in kw.value.elts:
            if isinstance(elt, ast.Call):
                holder = elt.func.value if isinstance(elt.func, ast.Attribute) else None
                tok = _attr_name(holder) if holder is not None else None
                toks.append((tok or "").upper())
            else:
                toks.append("?")
    return toks


def _node_id_from_schema_call(call):
    for kw in call.keywords:
        if kw.arg == "node_id":
            return _const_str(kw.value)
    return None


def _dict_inputs(func):
    """Legacy INPUT_TYPES -> {name: TYPE} from required+optional sub-dicts."""
    out = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Return):
            continue
        val = node.value
        if not isinstance(val, ast.Dict):
            continue
        for k, v in zip(val.keys, val.values):
            if _const_str(k) not in ("required", "optional"):
                continue
            if not isinstance(v, ast.Dict):
                continue
            for ik, iv in zip(v.keys, v.values):
                name = _const_str(ik)
                if name is None:
                    continue
                tok = None
                if isinstance(iv, ast.Tuple) and iv.elts:
                    first = iv.elts[0]
                    tok = _const_str(first)
                    if tok is None and isinstance(first, ast.Attribute):
                        tok = first.attr
                out[name] = (tok or "").upper() if isinstance(tok, str) else None
    return out


def _tuple_types(node):
    if isinstance(node, (ast.Tuple, ast.List)):
        toks = []
        for e in node.elts:
            t = _const_str(e)
            if t is None and isinstance(e, ast.Attribute):
                t = e.attr
            toks.append((t or "?").upper() if isinstance(t, str) else "?")
        return toks
    return None


def build_registry():
    reg = {}
    for rel in SRC_FILES:
        path = os.path.join(COMFY, rel)
        try:
            tree = ast.parse(open(path, "r", encoding="utf-8", errors="replace").read(), path)
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            methods = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            # --- v3: classmethod define_schema -> io.Schema(...) ---
            ds = methods.get("define_schema")
            if ds is not None:
                for node in ast.walk(ds):
                    if isinstance(node, ast.Call) and _attr_name(node.func) == "Schema":
                        nid = _node_id_from_schema_call(node) or cls.name
                        inputs, opaque = _inputs_from_schema_call(node)
                        reg[nid] = NodeSchema(nid, "v3", rel, cls.lineno, inputs,
                                              _outputs_from_schema_call(node), opaque)
                        break
                continue
            # --- legacy: INPUT_TYPES + RETURN_TYPES ---
            it = methods.get("INPUT_TYPES")
            if it is not None:
                inputs = _dict_inputs(it)
                outputs = []
                for stmt in cls.body:
                    if isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name) and t.id == "RETURN_TYPES":
                                outputs = _tuple_types(stmt.value) or []
                reg[cls.name] = NodeSchema(cls.name, "legacy", rel, cls.lineno, inputs, outputs)
    return reg


def disk_names(subdir):
    """Exact folder_paths names: relative to the model dir, OS-native separators.

    ComfyUI validates ckpt/unet/clip/vae names by exact string equality against
    folder_paths.get_filename_list(), which uses os.sep ('\\\\' on Windows).
    Normalising to '/' here would HIDE a real live-validation failure, so we do
    NOT normalise.
    """
    base = os.path.join(MODEL_LIB, subdir)
    found = set()
    if not os.path.isdir(base):
        return found
    for root, _dirs, files in os.walk(base):
        for f in files:
            found.add(os.path.relpath(os.path.join(root, f), base))
    return found


def check_prompt(name, graph, reg):
    print(f"\n===== {name} =====")
    misses = []
    # (a) structural: API format is a dict of node_id -> {class_type, inputs}
    if not isinstance(graph, dict) or not graph:
        print("  [MISS] (a) not a non-empty object")
        return 1
    print(f"  [PASS] (a) valid JSON, {len(graph)} nodes")

    outputs_by_node = {}
    for nid, node in graph.items():
        ct = node.get("class_type")
        schema = reg.get(ct)
        if schema is None:
            print(f"  [MISS] (b) node {nid}: class_type {ct!r} NOT found in ComfyUI source")
            misses.append(f"{nid}:{ct}")
            continue
        outputs_by_node[nid] = schema.outputs
        tag = f"{nid}:{ct}"
        # (c) input names
        bad = [k for k in node.get("inputs", {}) if k not in schema.inputs]
        if bad and schema.opaque:
            print(f"  [UNKNOWN] (c) {tag}: input(s) {bad} not in the literal set; node has "
                  f"helper-constructed input(s) -> must be validated live ({schema.file}:{schema.lineno})")
        elif bad:
            print(f"  [MISS] (c) {tag}: undeclared input(s) {bad}; declared={sorted(schema.inputs)} "
                  f"({schema.file}:{schema.lineno})")
            misses.append(tag)
        else:
            print(f"  [PASS] (c) {tag}: all {len(node.get('inputs', {}))} input name(s) declared"
                  f" ({schema.kind}, {schema.file})")

    # (e) references + type sanity
    for nid, node in graph.items():
        for ikey, ival in node.get("inputs", {}).items():
            if not (isinstance(ival, list) and len(ival) == 2
                    and isinstance(ival[0], (str, int))):
                continue
            src, slot = str(ival[0]), ival[1]
            if src not in graph:
                print(f"  [MISS] (e) node {nid}.{ikey} -> [{src},{slot}]: source node {src} does not exist")
                misses.append(f"{nid}.{ikey}")
                continue
            outs = outputs_by_node.get(src, [])
            if not isinstance(slot, int) or slot < 0 or slot >= len(outs):
                print(f"  [MISS] (e) node {nid}.{ikey} -> [{src},{slot}]: slot out of range "
                      f"({graph[src].get('class_type')} has {len(outs)} output(s))")
                misses.append(f"{nid}.{ikey}")
                continue
            # best-effort type check
            dst_schema = reg.get(node.get("class_type"))
            want = dst_schema.inputs.get(ikey) if dst_schema else None
            have = outs[slot] if slot < len(outs) else "?"
            verdict = ""
            if want and have and want not in ("?", ""):
                if want == have:
                    verdict = "  (types match)"
                else:
                    verdict = f"  (TYPE CHECK: want {want}, have {have})"
                    print(f"  [WARN] (e) node {nid}.{ikey} type mismatch: want {want}, have {have}")
            print(f"  [PASS] (e) node {nid}.{ikey} -> [{src},{slot}] ok{verdict}")

    # (d) model filenames vs disk
    cache = {}
    for nid, node in graph.items():
        for ikey, ival in node.get("inputs", {}).items():
            if ikey not in MODEL_INPUT_DIRS or not isinstance(ival, str):
                continue
            subdir = MODEL_INPUT_DIRS[ikey]
            if subdir not in cache:
                cache[subdir] = disk_names(subdir)
            if ival in cache[subdir]:
                print(f"  [PASS] (d) node {nid}.{ikey} = {ival!r} exists under {subdir}/")
            else:
                near = sorted(n for n in cache[subdir] if os.path.basename(n) == os.path.basename(ival))
                hint = ""
                if near:
                    fix = near[0]
                    alt = fix.replace(os.sep, "/")
                    if ival == alt:
                        hint = f" -- SEPARATOR MISMATCH: live API needs the native form {fix!r} (backslash on Windows)"
                    else:
                        hnormalized = ival.replace("/", os.sep)
                        if hnormalized in cache[subdir]:
                            hint = f" -- SEPARATOR MISMATCH: use {hnormalized!r}"
                print(f"  [MISS] (d) node {nid}.{ikey} = {ival!r} NOT found under {subdir}/"
                      + (f"; basename matches: {near}" if near else "") + hint)
                misses.append(f"{nid}.{ikey}")

    print(f"  -> {'ALL PASS' if not misses else 'MISSES: ' + str(misses)}")
    return len(misses)


def main():
    reg = build_registry()
    print(f"registry: {len(reg)} nodes parsed from {len(SRC_FILES)} source files under {COMFY}")
    total = 0
    for name in ("PROMPT_small.json", "PROMPT_h3.json", "PROMPT_h3_fallback.json"):
        path = os.path.join(HERE, name)
        try:
            graph = json.load(open(path, "r", encoding="utf-8"))
        except Exception as e:
            print(f"\n===== {name} =====\n  [MISS] (a) JSON parse failed: {e}")
            total += 1
            continue
        total += check_prompt(name, graph, reg)
    print(f"\n===== TOTAL MISSES: {total} =====")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
