#!/usr/bin/env python3
"""Tree-sitter parsing utilities for Rust/PyO3 extension analysis.

This is the core parsing module used by every analysis script in
rust-ext-review-toolkit. It provides structured access to Rust source code
via Tree-sitter, replacing fragile regex-based parsing.

Requires: pip install tree-sitter tree-sitter-rust

Grammar notes (verified against tree-sitter-rust):
  - Method calls are NOT a distinct node type. `obj.foo()` is a
    `call_expression` whose `function` child is a `field_expression`
    (`value:` = receiver, `field:` = method name). `find_calls_in_scope`
    normalises this so callers do not need to care.
  - `unsafe { ... }` is an `unsafe_block`; `unsafe fn` carries a
    `function_modifiers` child; `unsafe impl` carries a bare `unsafe`
    child token.
  - Attributes (`#[pyclass]`, ...) are `attribute_item` nodes that are
    *preceding siblings* of the item they decorate, not children.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

try:
    import tree_sitter
    import tree_sitter_rust
except ImportError:
    print(
        json.dumps(
            {
                "error": "tree-sitter not installed",
                "install": "pip install tree-sitter tree-sitter-rust",
            }
        )
    )
    sys.exit(1)

# Initialise the Rust parser once at module level.
RUST_LANGUAGE = tree_sitter.Language(tree_sitter_rust.language())
_parser = tree_sitter.Parser(RUST_LANGUAGE)

RUST_EXTENSIONS = frozenset({".rs"})

# tree-sitter-rust comment node types. The current grammar emits
# `line_comment` / `block_comment`; older grammars used a single `comment`.
_COMMENT_TYPES = frozenset({"line_comment", "block_comment", "comment"})


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_bytes(source_bytes: bytes) -> "tree_sitter.Tree":
    """Parse Rust source from bytes already in memory."""
    return _parser.parse(source_bytes)


def parse_string(source: str) -> "tree_sitter.Tree":
    """Parse a Rust source string."""
    return _parser.parse(source.encode("utf-8"))


def parse_file(path: Path) -> "tree_sitter.Tree":
    """Read a `.rs` file and parse it. Reads the bytes exactly once."""
    return _parser.parse(Path(path).read_bytes())


# --------------------------------------------------------------------------
# Generic node helpers
# --------------------------------------------------------------------------


def _root(tree_or_node: object) -> "tree_sitter.Node":
    """Accept either a Tree or a Node and return a Node to walk from."""
    return cast("tree_sitter.Node", getattr(tree_or_node, "root_node", tree_or_node))


def text_of(node: "tree_sitter.Node", source: bytes) -> str:
    """Return the source text for a node, decoded from bytes."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def node_line(node: "tree_sitter.Node") -> int:
    """Return the 1-indexed start line of a node."""
    return node.start_point[0] + 1


def walk(
    tree_or_node: object, type_filter: str | None = None
) -> Iterator["tree_sitter.Node"]:
    """Yield every descendant node (including the root), depth-first.

    Cursor-based to avoid deep Python recursion on large files. If
    ``type_filter`` is given, only nodes of that type are yielded.
    """
    cursor = _root(tree_or_node).walk()
    visited = False
    while True:
        if not visited:
            current = cursor.node
            assert current is not None  # cursor always points at a live node
            if type_filter is None or current.type == type_filter:
                yield current
            if cursor.goto_first_child():
                visited = False
                continue
        if cursor.goto_next_sibling():
            visited = False
            continue
        if cursor.goto_parent():
            visited = True
            continue
        break


def find_enclosing(
    node: "tree_sitter.Node", types: str | set[str]
) -> "tree_sitter.Node | None":
    """Walk ancestors of ``node`` and return the nearest one of a given type."""
    wanted = {types} if isinstance(types, str) else set(types)
    current = node.parent
    while current is not None:
        if current.type in wanted:
            return current
        current = current.parent
    return None


def strip_comments(source_bytes: bytes) -> bytes:
    """Blank out every comment, preserving byte offsets and line numbers.

    Comment bytes are replaced with spaces (newlines kept) so that line and
    column positions of the remaining code are unchanged. Use this before
    substring/regex checks on a body — a comment containing a keyword such
    as ``unsafe`` or ``unwrap`` otherwise causes false negatives.
    """
    tree = parse_bytes(source_bytes)
    out = bytearray(source_bytes)
    for node in walk(tree):
        if node.type not in _COMMENT_TYPES:
            continue
        for i in range(node.start_byte, node.end_byte):
            if out[i] != 0x0A:  # keep '\n'
                out[i] = 0x20  # ' '
    return bytes(out)


# --------------------------------------------------------------------------
# `use` statements
# --------------------------------------------------------------------------


def extract_use_statements(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract `use` declarations.

    Returns a list of dicts with keys:
      - path: str        — the imported path (best-effort, e.g. "pyo3::ffi")
      - alias: str|None  — local alias from `use ... as alias`
      - is_glob: bool    — `use a::b::*`
      - names: list[str] — leaf names brought into scope (for `use a::{b,c}`)
      - node, line
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "use_declaration"):
        arg = node.child_by_field_name("argument")
        if arg is None:
            continue
        entry: dict = {
            "path": "",
            "alias": None,
            "is_glob": False,
            "names": [],
            "node": node,
            "line": node_line(node),
        }
        if arg.type == "use_wildcard":
            entry["is_glob"] = True
            entry["path"] = text_of(arg, source).rstrip("*: ").rstrip("::")
        elif arg.type == "use_as_clause":
            path = arg.child_by_field_name("path")
            alias = arg.child_by_field_name("alias")
            entry["path"] = text_of(path, source) if path else ""
            entry["alias"] = text_of(alias, source) if alias else None
            if entry["path"]:
                entry["names"] = [entry["path"].rsplit("::", 1)[-1]]
        elif arg.type == "scoped_use_list":
            path_node = arg.child_by_field_name("path")
            entry["path"] = text_of(path_node, source) if path_node else ""
            lst = arg.child_by_field_name("list")
            if lst is not None:
                entry["names"] = [
                    text_of(c, source)
                    for c in lst.children
                    if c.type in ("identifier", "scoped_identifier")
                ]
        else:
            entry["path"] = text_of(arg, source)
            if entry["path"]:
                entry["names"] = [entry["path"].rsplit("::", 1)[-1]]
        results.append(entry)
    return results


# --------------------------------------------------------------------------
# Attributes (`#[pyclass]`, `#[pymethods]`, `#[new]`, ...)
# --------------------------------------------------------------------------


def _attribute_parts(attr_item: "tree_sitter.Node", source: bytes) -> dict:
    """Parse one `attribute_item` into {name, path, args_text, node, line}."""
    name = ""
    path = ""
    args_text = None
    attr = next((c for c in attr_item.children if c.type == "attribute"), None)
    if attr is not None:
        for child in attr.children:
            if child.type == "identifier":
                name = path = text_of(child, source)
            elif child.type == "scoped_identifier":
                path = text_of(child, source)
                name = path.rsplit("::", 1)[-1]
            elif child.type == "token_tree":
                args_text = text_of(child, source)
        args = attr.child_by_field_name("arguments")
        if args is not None:
            args_text = text_of(args, source)
    return {
        "name": name,
        "path": path,
        "args_text": args_text,
        "node": attr_item,
        "line": node_line(attr_item),
    }


def extract_attributes(node: "tree_sitter.Node", source: bytes) -> list[dict]:
    """Return the `#[...]` attributes decorating ``node``.

    Attributes are preceding siblings of the item, so this walks backward
    over `attribute_item` siblings. Result order matches source order.
    """
    attrs: list[dict] = []
    sibling = node.prev_sibling
    while sibling is not None:
        if sibling.type == "attribute_item":
            attrs.append(_attribute_parts(sibling, source))
        elif sibling.type in _COMMENT_TYPES:
            pass  # comments between attributes and the item are fine
        else:
            break
        sibling = sibling.prev_sibling
    attrs.reverse()
    return attrs


def _target_of_attribute(attr_item: "tree_sitter.Node") -> "tree_sitter.Node | None":
    """Return the item an `attribute_item` decorates (the next real sibling)."""
    sibling = attr_item.next_sibling
    while sibling is not None:
        if sibling.type == "attribute_item" or sibling.type in _COMMENT_TYPES:
            sibling = sibling.next_sibling
            continue
        return sibling
    return None


def extract_attribute_targets(
    tree_or_node: object, source: bytes, attr_name: str
) -> list[dict]:
    """Find every item carrying a given `#[attr_name]` attribute.

    Matches by the final path segment, so `#[pyo3::pyclass]` matches
    ``attr_name="pyclass"``. Returns dicts with keys:
      - attr: the parsed attribute dict (name, path, args_text, ...)
      - target: the decorated node
      - target_type: target.type
      - line: target's 1-indexed line
    """
    results: list[dict] = []
    for attr_item in walk(tree_or_node, "attribute_item"):
        parts = _attribute_parts(attr_item, source)
        if parts["name"] != attr_name:
            continue
        target = _target_of_attribute(attr_item)
        if target is None:
            continue
        results.append(
            {
                "attr": parts,
                "target": target,
                "target_type": target.type,
                "line": node_line(target),
            }
        )
    return results


# --------------------------------------------------------------------------
# Function items
# --------------------------------------------------------------------------


def _function_modifiers(fn_node: "tree_sitter.Node", source: bytes) -> dict:
    """Inspect a function_item's `function_modifiers` child."""
    info: dict[str, bool | str | None] = {
        "is_unsafe": False,
        "is_async": False,
        "extern_abi": None,
    }
    mods = next((c for c in fn_node.children if c.type == "function_modifiers"), None)
    if mods is None:
        return info
    for child in mods.children:
        if child.type == "unsafe":
            info["is_unsafe"] = True
        elif child.type == "async":
            info["is_async"] = True
        elif child.type == "extern_modifier":
            abi = next((c for c in child.children if c.type == "string_literal"), None)
            info["extern_abi"] = (
                text_of(abi, source).strip('"') if abi is not None else ""
            )
    return info


def extract_fn_items(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract every `fn` item — free functions and methods inside impls.

    Returns dicts with keys:
      - name: str
      - node: the function_item node
      - params_node, body_node: tree_sitter.Node | None
      - return_type: str | None  (text after `->`)
      - is_unsafe, is_async: bool
      - extern_abi: str | None   (e.g. "C" for `extern "C" fn`)
      - start_line, end_line
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "function_item"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        ret = node.child_by_field_name("return_type")
        mods = _function_modifiers(node, source)
        results.append(
            {
                "name": text_of(name_node, source),
                "node": node,
                "params_node": node.child_by_field_name("parameters"),
                "body_node": node.child_by_field_name("body"),
                "return_type": text_of(ret, source) if ret is not None else None,
                "is_unsafe": mods["is_unsafe"],
                "is_async": mods["is_async"],
                "extern_abi": mods["extern_abi"],
                "start_line": node_line(node),
                "end_line": node.end_point[0] + 1,
            }
        )
    return results


# --------------------------------------------------------------------------
# Impl blocks
# --------------------------------------------------------------------------


def extract_impl_blocks(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract `impl` blocks.

    Returns dicts with keys:
      - type: str         — the impl target type (e.g. "Counter")
      - trait: str | None — for `impl Trait for Type`
      - is_unsafe: bool   — `unsafe impl`
      - body_node: the declaration_list, or None
      - node, start_line, end_line
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "impl_item"):
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        is_unsafe = any(c.type == "unsafe" for c in node.children)
        results.append(
            {
                "type": text_of(type_node, source) if type_node else "",
                "trait": text_of(trait_node, source) if trait_node else None,
                "is_unsafe": is_unsafe,
                "body_node": node.child_by_field_name("body"),
                "node": node,
                "start_line": node_line(node),
                "end_line": node.end_point[0] + 1,
            }
        )
    return results


# --------------------------------------------------------------------------
# Struct definitions
# --------------------------------------------------------------------------


def extract_struct_defs(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract `struct` definitions.

    Returns dicts with keys:
      - name: str
      - fields: list of {name, type, node, line}  (named fields)
      - is_tuple_struct, is_unit_struct: bool
      - node, start_line
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "struct_item"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        body = node.child_by_field_name("body")
        fields: list[dict] = []
        is_tuple = is_unit = False
        if body is None:
            is_unit = True
        elif body.type == "ordered_field_declaration_list":
            is_tuple = True
            for child in body.children:
                if child.type != "field_declaration":
                    continue
                ftype = child.child_by_field_name("type")
                fields.append(
                    {
                        "name": None,
                        "type": text_of(ftype, source)
                        if ftype is not None
                        else text_of(child, source),
                        "node": child,
                        "line": node_line(child),
                    }
                )
        else:  # field_declaration_list
            for child in body.children:
                if child.type != "field_declaration":
                    continue
                fname = child.child_by_field_name("name")
                ftype = child.child_by_field_name("type")
                fields.append(
                    {
                        "name": text_of(fname, source) if fname else None,
                        "type": text_of(ftype, source) if ftype else "",
                        "node": child,
                        "line": node_line(child),
                    }
                )
        results.append(
            {
                "name": text_of(name_node, source),
                "fields": fields,
                "is_tuple_struct": is_tuple,
                "is_unit_struct": is_unit,
                "node": node,
                "start_line": node_line(node),
            }
        )
    return results


# --------------------------------------------------------------------------
# Enum definitions
# --------------------------------------------------------------------------


def extract_enum_defs(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract `enum` definitions.

    Returns dicts with keys:
      - name: str
      - variants: list of {name, kind, fields, node, line}
          - kind: "unit" | "tuple" | "struct"
          - fields: list of {name, type, node, line}  (None name for tuple variants)
      - all_fields: list of {name, type, node, line, variant}  (flattened across
        variants -- convenient for "does this enum hold a Py handle anywhere?"
        checks without re-walking variants)
      - node, start_line

    Mirrors `extract_struct_defs` so downstream consumers can treat structs and
    enums uniformly when the question is shape-of-data, not shape-of-name.
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "enum_item"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        body = node.child_by_field_name("body")
        variants: list[dict] = []
        all_fields: list[dict] = []
        if body is not None:
            for child in body.children:
                if child.type != "enum_variant":
                    continue
                vname_node = child.child_by_field_name("name")
                vname = text_of(vname_node, source) if vname_node is not None else ""
                vbody = child.child_by_field_name("body")
                vfields: list[dict] = []
                kind = "unit"
                if vbody is not None and vbody.type == "ordered_field_declaration_list":
                    kind = "tuple"
                    # In an enum tuple variant the types are *direct* children
                    # of the ordered_field_declaration_list (each carrying
                    # field_name "type"). In a tuple struct they are wrapped
                    # in `field_declaration` nodes. The grammar differs from
                    # `extract_struct_defs`, so iterate over typed children
                    # rather than filtering on `field_declaration`.
                    for fchild in vbody.children:
                        if fchild.type in ("(", ")", ","):
                            continue
                        # Most variant fields are direct type nodes. Some
                        # private/visibility-qualified variants may still wrap
                        # in `field_declaration`; handle both.
                        if fchild.type == "field_declaration":
                            ftype = fchild.child_by_field_name("type")
                            ttext = (
                                text_of(ftype, source)
                                if ftype is not None
                                else text_of(fchild, source)
                            )
                        else:
                            ttext = text_of(fchild, source)
                        vfields.append(
                            {
                                "name": None,
                                "type": ttext,
                                "node": fchild,
                                "line": node_line(fchild),
                            }
                        )
                elif vbody is not None and vbody.type == "field_declaration_list":
                    kind = "struct"
                    for fchild in vbody.children:
                        if fchild.type != "field_declaration":
                            continue
                        fname = fchild.child_by_field_name("name")
                        ftype = fchild.child_by_field_name("type")
                        vfields.append(
                            {
                                "name": text_of(fname, source) if fname else None,
                                "type": text_of(ftype, source) if ftype else "",
                                "node": fchild,
                                "line": node_line(fchild),
                            }
                        )
                variants.append(
                    {
                        "name": vname,
                        "kind": kind,
                        "fields": vfields,
                        "node": child,
                        "line": node_line(child),
                    }
                )
                for f in vfields:
                    all_fields.append({**f, "variant": vname})
        results.append(
            {
                "name": text_of(name_node, source),
                "variants": variants,
                "all_fields": all_fields,
                "node": node,
                "start_line": node_line(node),
            }
        )
    return results


# --------------------------------------------------------------------------
# Unsafe blocks and unsafe fns
# --------------------------------------------------------------------------


def extract_unsafe_blocks(tree_or_node: object, source: bytes) -> list[dict]:
    """Extract `unsafe { ... }` blocks and `unsafe fn` definitions.

    Returns dicts with keys:
      - kind: "block" | "fn"
      - node: the unsafe_block or function_item node
      - body_node: the inner block
      - function: name of the enclosing fn (for blocks), or own name (for fns)
      - start_line, end_line
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "unsafe_block"):
        body = next((c for c in node.children if c.type == "block"), None)
        enclosing = find_enclosing(node, "function_item")
        fn_name = None
        if enclosing is not None:
            nm = enclosing.child_by_field_name("name")
            fn_name = text_of(nm, source) if nm else None
        results.append(
            {
                "kind": "block",
                "node": node,
                "body_node": body,
                "function": fn_name,
                "start_line": node_line(node),
                "end_line": node.end_point[0] + 1,
            }
        )
    for node in walk(tree_or_node, "function_item"):
        if not _function_modifiers(node, source)["is_unsafe"]:
            continue
        nm = node.child_by_field_name("name")
        results.append(
            {
                "kind": "fn",
                "node": node,
                "body_node": node.child_by_field_name("body"),
                "function": text_of(nm, source) if nm else None,
                "start_line": node_line(node),
                "end_line": node.end_point[0] + 1,
            }
        )
    results.sort(key=lambda r: r["node"].start_byte)
    return results


# --------------------------------------------------------------------------
# Calls (function calls and method calls)
# --------------------------------------------------------------------------


def _resolve_callee(
    fn_node: "tree_sitter.Node", source: bytes
) -> tuple[str, str, "tree_sitter.Node | None"]:
    """Resolve a call_expression's `function` child.

    Returns (kind, name, receiver_node):
      - identifier        -> ("function", "compute", None)
      - scoped_identifier -> ("path", "raw::Py_None", None)
      - field_expression  -> ("method", "call_method0", <receiver node>)
      - generic_function  -> resolved from its inner `function`
    """
    t = fn_node.type
    if t == "identifier":
        return ("function", text_of(fn_node, source), None)
    if t == "scoped_identifier":
        return ("path", text_of(fn_node, source), None)
    if t == "field_expression":
        field = fn_node.child_by_field_name("field")
        value = fn_node.child_by_field_name("value")
        name = text_of(field, source) if field is not None else ""
        return ("method", name, value)
    if t == "generic_function":
        inner = fn_node.child_by_field_name("function")
        if inner is not None and inner is not fn_node:
            return _resolve_callee(inner, source)
    return ("other", text_of(fn_node, source), None)


def find_calls_in_scope(
    tree_or_node: object, source: bytes, fn_names: set[str] | None = None
) -> list[dict]:
    """Find every call expression within a scope.

    Method calls (`obj.foo()`) and function calls (`foo()`, `a::b()`) are
    normalised into one shape. Returns dicts with keys:
      - name: str          — function or method name (no path/receiver)
      - kind: "function" | "path" | "method" | "other"
      - full_callee: str   — the raw callee text
      - receiver_text: str | None  — for method calls
      - receiver_node: tree_sitter.Node | None
      - args_text: str     — argument list without the outer parens
      - node: the call_expression node
      - line, start_byte
    If ``fn_names`` is given, only calls whose `name` is in the set are kept.
    """
    results: list[dict] = []
    for node in walk(tree_or_node, "call_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node is None:
            continue
        kind, full, receiver = _resolve_callee(fn_node, source)
        name = full.rsplit("::", 1)[-1] if kind == "path" else full
        if fn_names is not None and name not in fn_names and full not in fn_names:
            continue
        args = node.child_by_field_name("arguments")
        args_text = ""
        if args is not None:
            args_text = text_of(args, source).strip()
            if args_text.startswith("(") and args_text.endswith(")"):
                args_text = args_text[1:-1].strip()
        results.append(
            {
                "name": name,
                "kind": kind,
                "full_callee": full,
                "receiver_text": text_of(receiver, source) if receiver else None,
                "receiver_node": receiver,
                "args_text": args_text,
                "node": node,
                "line": node_line(node),
                "start_byte": node.start_byte,
            }
        )
    return results


# --------------------------------------------------------------------------
# Closures
# --------------------------------------------------------------------------


def find_closure_bodies(
    tree_or_node: object,
    source: bytes,
    callee: str | set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Find closure expressions and their bodies.

    If ``callee`` is given (a name or set of names), only closures passed as
    an argument to a call of that callee are returned — e.g.
    ``find_closure_bodies(fn, src, callee={"detach", "allow_threads"})``
    finds the closures handed to `py.detach(...)` / `py.allow_threads(...)`.

    Returns dicts with keys:
      - body_node: the closure body (an expression or a block)
      - params_text: str
      - is_move: bool
      - callee: str | None  — the enclosing call's callee name, if any
      - node, line
    """
    wanted = None
    if callee is not None:
        wanted = {callee} if isinstance(callee, str) else set(callee)
    results: list[dict] = []
    for node in walk(tree_or_node, "closure_expression"):
        enclosing_callee = None
        parent = node.parent
        if parent is not None and parent.type == "arguments":
            call = parent.parent
            if call is not None and call.type == "call_expression":
                fn_node = call.child_by_field_name("function")
                if fn_node is not None:
                    _, full, _ = _resolve_callee(fn_node, source)
                    enclosing_callee = full.rsplit("::", 1)[-1]
        if wanted is not None and enclosing_callee not in wanted:
            continue
        params = node.child_by_field_name("parameters")
        results.append(
            {
                "body_node": node.child_by_field_name("body"),
                "params_text": text_of(params, source) if params else "",
                "is_move": any(c.type == "move" for c in node.children),
                "callee": enclosing_callee,
                "node": node,
                "line": node_line(node),
            }
        )
    return results


# --------------------------------------------------------------------------
# Generic parameters and trait bounds
# --------------------------------------------------------------------------


def extract_generic_params(node: "tree_sitter.Node", source: bytes) -> list[dict]:
    """Extract generic parameters declared on a fn / impl / struct node.

    Returns dicts with keys:
      - name: str
      - kind: "type" | "lifetime" | "const"
      - bounds: list[str]  — declared trait/lifetime bounds
    """
    results: list[dict] = []
    params = node.child_by_field_name("type_parameters")
    if params is None:
        return results
    for child in params.children:
        if child.type == "lifetime":
            results.append(
                {"name": text_of(child, source), "kind": "lifetime", "bounds": []}
            )
        elif child.type == "type_identifier":
            results.append(
                {"name": text_of(child, source), "kind": "type", "bounds": []}
            )
        elif child.type == "constrained_type_parameter":
            left = child.child_by_field_name("left")
            bound = child.child_by_field_name("bound")
            bounds = []
            if bound is not None:
                bounds = [
                    text_of(b, source)
                    for b in bound.children
                    if b.type
                    in ("type_identifier", "scoped_type_identifier", "lifetime")
                ]
            results.append(
                {
                    "name": text_of(left, source) if left else "",
                    "kind": "type",
                    "bounds": bounds,
                }
            )
        elif child.type == "const_parameter":
            nm = child.child_by_field_name("name")
            results.append(
                {
                    "name": text_of(nm, source) if nm else "",
                    "kind": "const",
                    "bounds": [],
                }
            )
    return results


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def has_parse_errors(tree: "tree_sitter.Tree") -> bool:
    """True if the parse tree contains ERROR or MISSING nodes."""
    return tree.root_node.has_error
