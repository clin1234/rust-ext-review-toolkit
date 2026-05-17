# Tree-sitter-rust Recipes

Concrete patterns for the scanners in the design doc. Open this while you're
writing scripts — it's a reference, not a read-once.

**Inputs to this document:**

- `rust-ext-review-toolkit-design.md` §3.2 (per-agent patterns to detect)
- `PYO3_PRIMER.md` (what each pattern means in PyO3)

**This document covers:**

- §1: What tree-sitter-rust sees vs doesn't
- §2: The node types you'll work with
- §3: Recipes per scanner (mostly walker patterns; ts-queries where they're
  cleaner)
- §4: Common pitfalls
- §5: Fixture-driven test approach

---

## 1. What tree-sitter-rust can see — and what it can't

### Can see (clearly)

- Function items (`fn foo(...) -> T { ... }`) and method items inside `impl`
- `impl Trait for Type` and inherent `impl Type` blocks
- Struct and enum definitions, including field types
- Attribute macros at the *call site* (`#[pyclass]`, `#[pymethods]`,
  `#[pyfunction]`, `#[pymodule]`, `#[new]`, `#[getter]`, etc.)
- `unsafe { ... }` blocks and `unsafe fn` modifiers
- Closure expressions and their bodies
- `use` statements
- Generic parameters and trait bounds in `where` clauses
- Lifetime annotations on types (`Bound<'py, T>`, `&'a Bound<'py, T>`)
- Function calls, method calls, and field accesses
- Match expressions and arms
- The string content of attribute arguments (sort of — see §4.1)

### Can NOT see

- **Expanded procedural macro output.** `#[pyclass]` expands into ~hundred
  lines of generated code (the `PyClassImpl` impl, the slot tables, the
  init helpers). Tree-sitter shows you the user's struct + attribute; the
  expanded code is not in the syntax tree.
- **Resolved trait satisfaction.** "Does `MyType: Send` hold?" — you can
  see the *declared* bound (`where T: Send`) but not whether a concrete
  `MyType` actually satisfies it. You infer from field types using
  `data/non_send_sync_types.json`.
- **Resolved import targets.** `use foo::bar;` followed by `bar()` — you
  see the call as `bar(...)` and `foo::bar` is what was imported, but
  cross-module resolution isn't tree-sitter's job. Use string matching +
  the `use` table from `extract_use_statements()`.
- **Macro-generated identifiers.** `paste!` and friends produce identifiers
  the parser doesn't see. Rare in PyO3 user code.
- **Type inference.** A `let x = some_call();` — you don't know what type
  `x` is. Heuristics only.

### Workarounds for the visibility gaps

- For trait-bound satisfaction: encode known-non-Send patterns in
  `data/non_send_sync_types.json`. Accept that generic-parameter cases will be
  missed.
- For trait method calls (e.g., is `obj.something()` returning `PyResult`?):
  heuristic — if the receiver is typed as `Bound<'py, T>` or `Py<T>` or a
  PyO3 path, treat the call as fallible. The agent triage step catches the
  false positives.
- For macro expansion: opportunistic `cargo expand` (design §10.1, deferred).
  For v0.1, just accept the visibility gap and document it in each scanner's
  output ("inferred from source; macro-expansion may show more").

---

## 2. Node types in tree-sitter-rust

Reference: <https://github.com/tree-sitter/tree-sitter-rust/blob/master/src/node-types.json>

The node types you'll touch most often:

| Node type | What it represents | Children of interest |
|-----------|---------------------|----------------------|
| `source_file` | Top-level file | `function_item`, `impl_item`, `struct_item`, `mod_item`, `use_declaration`, attribute-decorated items |
| `function_item` | A `fn` declaration | `function_modifiers` (for `unsafe`/`async`/`const`), `identifier` (name), `parameters`, `type` (return type), `block` (body) |
| `impl_item` | An `impl` block | `type` (target type), `where_clause`, `declaration_list` (body containing methods) |
| `struct_item` | A struct definition | `type_identifier` (name), `field_declaration_list`, `type_parameters` |
| `field_declaration` | A struct field | `field_identifier` (name), `type` (the field's type) |
| `attribute_item` | An attribute like `#[pyclass]` | `attribute` (the inner content) |
| `attribute` | Inside `#[...]` | `identifier` (path) + optional `token_tree` (args) |
| `unsafe_block` | `unsafe { ... }` | `block` |
| `function_modifiers` | `pub`, `async`, `unsafe`, `const`, etc. modifiers | child token type indicates which |
| `closure_expression` | `\|a, b\| { ... }` or `move \|a, b\| { ... }` | `closure_parameters`, `block` or expression body |
| `call_expression` | `foo()`, `Bar::baz(arg)`, **and method calls** `obj.m(arg)` | `function` (callee) + `arguments`. The callee is an `identifier`, a `scoped_identifier`, or — for a method call — a `field_expression` (`value` = receiver, `field` = method name). **There is no `method_call_expression` node**; use `rust_ts_utils.find_calls_in_scope`, which normalises all three forms. |
| `field_expression` | `obj.field` | `expression` (receiver), `field_identifier` (field name) |
| `type_identifier` | A type name like `PyDict` | (leaf, has `.text`) |
| `scoped_type_identifier` | `pyo3::types::PyDict` | path segments |
| `generic_type` | `Bound<'py, PyDict>` | `type` + `type_arguments` (containing lifetimes + types) |
| `reference_type` | `&PyAny`, `&'py PyDict` | optional `lifetime`, `type` |
| `lifetime` | `'py`, `'a`, `'static` | (leaf with the lifetime name) |
| `use_declaration` | `use pyo3::prelude::*;` | path segments |
| `let_declaration` | `let x = expr;` | `pattern`, optional `type`, optional `value` |
| `match_expression` | `match x { ... }` | `expression` (scrutinee), `match_block` |
| `match_arm` | One arm of a match | `match_pattern`, `expression` or `block` |
| `return_expression` | `return foo;` | the returned expression |
| `try_expression` | `expr?` | the inner expression |
| `block` | `{ stmts }` | sequence of statements + optional final expression |
| `expression_statement` | An expression terminated by `;` | the expression |
| `macro_invocation` | `foo!(args)` | identifier + token tree |
| `line_comment` / `block_comment` | `// ...` and `/* ... */` | (leaf; text includes the delimiter). There is no single `comment` type — match `node.type.endswith("comment")`. |

The Python `tree_sitter` API gives you nodes via `tree.root_node`, recursive
`.children`, and `.child_by_field_name("name")` for named children.

Always work in **bytes** for `source` — `node.start_byte` / `node.end_byte`
slice the source bytes; converting to `str` early loses byte offsets.

---

## 3. Per-scanner recipes

Each subsection: the patterns to detect, the node-walking approach, and a
test fixture pattern.

### 3.1 `scan_unsafe_blocks.py`

#### Pattern 1: every unsafe block + unsafe fn

Walk the tree for two node types:

```python
def extract_unsafe_blocks(tree: Tree, source: bytes) -> list[UnsafeNode]:
    results = []
    # unsafe { ... } blocks
    for node in walk(tree.root_node):
        if node.type == "unsafe_block":
            results.append(UnsafeNode(
                kind="block",
                node=node,
                source_text=source[node.start_byte:node.end_byte].decode("utf-8"),
                start_line=node.start_point[0] + 1,
            ))
        elif node.type == "function_item":
            # Check function_modifiers for `unsafe`
            for modifier_node in node.child_by_field_name("modifiers") or []:
                if source[modifier_node.start_byte:modifier_node.end_byte] == b"unsafe":
                    results.append(UnsafeNode(
                        kind="fn",
                        node=node,
                        ...
                    ))
                    break
    return results
```

#### Pattern 2: GIL availability check

For each unsafe block, walk ancestors to find a parameter of type
`Python<'py>` (or a closure parameter receiving one):

```python
def has_python_in_scope(unsafe_node: Node, source: bytes) -> bool:
    # Walk up to find the containing fn or closure
    cur = unsafe_node.parent
    while cur is not None:
        if cur.type in ("function_item", "closure_expression"):
            # Look at parameters for Python<'py> type
            params = cur.child_by_field_name("parameters")
            if params and contains_python_param(params, source):
                return True
        cur = cur.parent
    return False

def contains_python_param(params: Node, source: bytes) -> bool:
    # Match parameter patterns whose type contains "Python<'"
    text = source[params.start_byte:params.end_byte].decode("utf-8")
    return "Python<'" in text  # heuristic; refine if needed
```

The heuristic is approximate but adequate. False positives (e.g., a comment
with "Python<'") are filtered by checking the parameter list's structure
rather than the whole `params` text.

#### Pattern 3: pyo3-ffi calls

Walk the unsafe block body for `call_expression` whose callee starts with
`pyo3_ffi::` or `ffi::` (when `use pyo3::ffi;` is in scope):

```python
def find_pyo3_ffi_calls(unsafe_node: Node, source: bytes,
                       ffi_aliases: set[str]) -> list:
    calls = []
    for node in walk(unsafe_node):
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            callee_text = source[callee.start_byte:callee.end_byte].decode("utf-8")
            if any(callee_text.startswith(alias + "::") for alias in ffi_aliases):
                calls.append({
                    "callee": callee_text,
                    "line": callee.start_point[0] + 1,
                })
    return calls

# Build ffi_aliases from `use` statements: { "ffi", "pyo3_ffi", "pyo3::ffi" }
```

Cross-reference each callee against `data/api_tables.json` (vendored from
cext) to apply borrowed/new/stolen-ref reasoning. The cext-tool's
`scan_refcounts.py` logic translates directly: if you see
`ffi::PyList_GetItem`, the result is a borrowed ref; if you see
`ffi::PyList_SetItem`, the value arg is stolen.

#### Pattern 4: `transmute` between handle types

```python
def find_transmute_calls(unsafe_node: Node, source: bytes) -> list:
    matches = []
    for node in walk(unsafe_node):
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            callee_text = source[callee.start_byte:callee.end_byte].decode("utf-8")
            # std::mem::transmute, mem::transmute, transmute
            if callee_text.endswith("transmute"):
                # Check the turbofish or generic args for PyO3 handle types
                args = source[node.start_byte:node.end_byte].decode("utf-8")
                if any(handle in args for handle in (
                    "Bound<", "Py<", "Borrowed<", "PyAny", "PyDict", "PyList",
                )):
                    matches.append({...})
    return matches
```

#### Pattern 5: `unsafe impl Send` / `unsafe impl Sync`

Walk top-level items for `impl_item` with `unsafe` modifier and trait
identifier `Send` or `Sync`:

```python
def find_unsafe_send_sync_impls(tree: Tree, source: bytes) -> list:
    results = []
    for node in walk(tree.root_node):
        if node.type != "impl_item":
            continue
        if not has_unsafe_modifier(node, source):
            continue
        # impl_item has a `trait` field naming the trait being implemented
        trait_node = node.child_by_field_name("trait")
        if trait_node is None:
            continue
        trait_text = source[trait_node.start_byte:trait_node.end_byte].decode("utf-8")
        if trait_text in ("Send", "Sync"):
            type_node = node.child_by_field_name("type")
            type_text = source[type_node.start_byte:type_node.end_byte].decode("utf-8")
            results.append({
                "trait": trait_text,
                "for_type": type_text,
                "line": node.start_point[0] + 1,
                "has_safety_comment": preceding_comment_contains_safety(node, source),
            })
    return results
```

#### Test fixtures for `scan_unsafe_blocks`

```python
FIXTURE_UNSAFE_FFI_CALL = """\
use pyo3::ffi;

unsafe fn bump<'py>(py: pyo3::Python<'py>, obj: pyo3::Bound<'py, pyo3::PyAny>) {
    ffi::Py_IncRef(obj.as_ptr());
}
"""

FIXTURE_UNSAFE_TRANSMUTE = """\
fn bad(obj: pyo3::Py<pyo3::PyAny>) -> pyo3::Bound<'static, pyo3::PyAny> {
    unsafe { std::mem::transmute(obj) }
}
"""

FIXTURE_UNSAFE_SEND_IMPL = """\
struct Holder { inner: std::rc::Rc<u8> }
unsafe impl Send for Holder {}
"""

FIXTURE_UNSAFE_WITH_SAFETY = """\
unsafe fn ok<'py>(py: pyo3::Python<'py>, obj: pyo3::Bound<'py, pyo3::PyAny>) {
    // SAFETY: obj is non-null and we hold the GIL.
    pyo3::ffi::Py_IncRef(obj.as_ptr());
}
"""
```

Tests should assert the appropriate finding counts and absence-of-finding
on the safety-commented fixture.

---

### 3.2 `scan_pyresult.py`

The challenge: there's no type inference. You have to infer "this expression
returns `PyResult<T>`" syntactically.

#### Pattern 1: `.unwrap()` / `.expect()` on a PyO3-ish receiver

```python
def find_unwrap_on_pyresult(tree: Tree, source: bytes) -> list:
    findings = []
    # find_calls_in_scope normalises every call. Method calls come back with
    # kind == "method", `name` = the method, and `receiver_text` populated.
    for call in find_calls_in_scope(tree, source):
        if call["kind"] != "method" or call["name"] not in ("unwrap", "expect"):
            continue
        receiver = call["receiver_text"] or ""
        # Check ancestor chain — are we inside a #[pymethods]/#[pyfunction] body?
        if looks_like_pyresult(receiver, source) and in_pyo3_callable(
            call["node"], source
        ):
            findings.append({
                "type": "unwrap_on_pyresult",
                "line": call["line"],
                "receiver": receiver,
            })
    return findings

def looks_like_pyresult(receiver_text: str, source: bytes) -> bool:
    # Heuristic: receiver is a chained call where any element looks like PyO3
    pyo3_hints = (
        ".call_method", ".call0", ".call1", ".getattr", ".setattr",
        ".import", ".import_bound", ".bind", ".extract", ".downcast", ".cast",
        ".get_item", ".set_item", ".del_item", ".len()",
        "::new_bound", "PyResult",
    )
    return any(hint in receiver_text for hint in pyo3_hints)

def in_pyo3_callable(node: Node, source: bytes) -> bool:
    # Walk ancestors; if you hit a function_item whose preceding attribute
    # is #[pyfunction] / #[pymethods] / #[new] / #[getter] / #[setter] etc.,
    # return True. Stop at top-level.
    ...
```

The `looks_like_pyresult` heuristic intentionally has false positives. The
agent triage step is responsible for separating "in PyO3 callable" from
"in a helper called from PyO3 callable" from "in a test helper".

#### Pattern 2: `.ok()` and `let _ =`

```python
def find_swallowed_pyresult(tree: Tree, source: bytes) -> list:
    findings = []
    # Pattern A: .ok() on a PyO3-ish receiver
    for call in find_calls_in_scope(tree, source):
        if call["kind"] == "method" and call["name"] == "ok":
            if looks_like_pyresult(call["receiver_text"] or "", source):
                findings.append({"type": "ok_on_pyresult", "line": call["line"], ...})

    # Pattern B: let _ = <pyresult-expr>
    for node in walk(tree.root_node):
        if node.type != "let_declaration":
            continue
        pattern = node.child_by_field_name("pattern")
        value = node.child_by_field_name("value")
        if pattern is None or value is None:
            continue
        if source[pattern.start_byte:pattern.end_byte] == b"_":
            value_text = source[value.start_byte:value.end_byte].decode()
            if looks_like_pyresult(value_text, source):
                findings.append({"type": "let_underscore_pyresult", ...})
    return findings
```

#### Pattern 3: missing `?` operator (heuristic)

The hardest pattern. Heuristic: in a fn returning `PyResult<T>`, find
let-bindings whose RHS looks like `PyResult` and whose pattern is *not* a
`?`-shortcircuited let-else.

Realistic v0.1 approach: detect the *explicit-wrong* cases (the `Result`
ends up shadowed without `?`), accept that the *implicit-wrong* cases
(`PyResult` returned to a variable then ignored) are partially covered by
the `.ok()` / `let _ =` rules above.

#### Pattern 4: exception clobbering

```rust
let err = call_a(py).err();   // captured PyErr
call_b(py)?;                  // another fallible Python API
if let Some(e) = err {
    return Err(e);
}
```

Detection: find any `let <pattern> = <expr>.err();` where `<expr>` looks
like PyO3. After that `let`, find the next Python API call that can fail.
Flag if anything fallible happens before the `err` is propagated.

This is genuinely hard to detect without dataflow. Ship a conservative
version: detect the explicit `.err()` capture pattern; let the agent
triage step look at the surrounding 30 lines.

#### Test fixtures

```python
FIXTURE_UNWRAP_IN_PYFN = """\
use pyo3::prelude::*;

#[pyfunction]
fn bad(py: Python<'_>) -> PyObject {
    py.import_bound("json").unwrap().getattr("dumps").unwrap().into()
}
"""

FIXTURE_OK_SWALLOW = """\
fn helper<'py>(obj: pyo3::Bound<'py, pyo3::PyAny>) {
    let _ = obj.call_method0("close").ok();
}
"""
```

---

### 3.3 `scan_gil_discipline.py`

**Vocabulary note.** PyO3 0.25 renamed the GIL API: `with_gil`→`attach`,
`allow_threads`→`detach`. The scanner must recognize **both** name sets. Define
them once:

```python
DETACH_NAMES = {"detach", "allow_threads"}    # GIL-release closure
ATTACH_NAMES = {"attach", "with_gil"}         # GIL-acquire closure
ATTACH_PATHS = ("Python::attach", "Python::with_gil")
```

#### Pattern 1: closure body for `detach` (legacy `allow_threads`)

`rust_ts_utils.find_closure_bodies` finds closures and can filter them by the
callee they are passed to — so it locates the closures handed to `detach` /
`allow_threads` in one call. Walk each closure body for forbidden expressions:

```python
def find_detach_violations(tree: Tree, source: bytes) -> list:
    findings = []
    for closure in find_closure_bodies(tree, source, callee=DETACH_NAMES):
        violations = scan_for_gil_violations(closure["body_node"], source)
        if violations:
            findings.append({"line": closure["line"], "violations": violations})
    return findings

def scan_for_gil_violations(body: Node, source: bytes) -> list:
    # Walk body for:
    # - method calls .bind(py)
    # - method calls on receivers typed Bound<'py, T>
    # - pyo3-ffi calls
    # - any use of an outer Python<'py> token
    ...
```

#### Pattern 2: nested `attach` (legacy `with_gil`)

Find `call_expression` whose callee is `Python::attach` / `Python::with_gil`
(or scoped variants). Check ancestor closures for an outer attach or an outer
function with a `Python<'py>` parameter:

```python
def find_nested_attach(tree: Tree, source: bytes) -> list:
    findings = []
    for node in walk(tree.root_node):
        if node.type != "call_expression":
            continue
        callee_text = node_text(node.child_by_field_name("function"), source)
        if not any(p in callee_text for p in ATTACH_PATHS):
            continue
        if has_outer_attach_or_python_param(node, source):
            findings.append({...})
    return findings
```

#### Pattern 3: `extern "C" fn` without `attach`

```python
def find_extern_c_pyo3_unsafe(tree: Tree, source: bytes) -> list:
    findings = []
    for node in walk(tree.root_node):
        if node.type != "function_item":
            continue
        # Check for `extern "C"` modifier
        modifiers = node.child_by_field_name("modifiers")
        if modifiers is None or b'extern "C"' not in source[modifiers.start_byte:modifiers.end_byte]:
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        if uses_pyo3_apis(body, source) and not starts_with_attach(body, source):
            findings.append({...})
    return findings
```

`uses_pyo3_apis` is a string-match heuristic: any `pyo3::`, `Python::`,
`Bound<`, `Py<`, `.bind(`, etc. inside the body.

#### Test fixtures

```python
FIXTURE_BIND_IN_DETACH = """\
use pyo3::prelude::*;

fn dangerous<'py>(py: Python<'py>, obj: Py<PyAny>) {
    py.detach(|| {
        let _b = obj.bind(py);
    });
}
"""

# Legacy-vocabulary twin — the scanner must catch this too (pre-0.25 crates).
FIXTURE_BIND_IN_ALLOW_THREADS = FIXTURE_BIND_IN_DETACH.replace(
    ".detach(", ".allow_threads("
)
```

(This is a compile error in safe code; the scanner works on unparsed source,
so it keys on the syntactic pattern. Include both vocabularies as fixtures.)

---

### 3.4 `scan_panic_safety.py`

Walk `#[pymethods]` impl blocks and `#[pyfunction]` items. For each method,
scan body for panic-prone expressions.

```python
def panic_prone_patterns(node: Node, source: bytes,
                         panic_apis: dict) -> list:
    findings = []
    # .unwrap() / .expect() — method calls, via the normalised call list.
    for call in find_calls_in_scope(node, source):
        if call["kind"] == "method" and call["name"] in ("unwrap", "expect"):
            findings.append(
                {"type": f"{call['name']}_call", "line": call["line"], ...}
            )

    for n in walk(node):
        # Index expressions on slices/vecs — these panic on OOB
        if n.type == "index_expression":
            findings.append({"type": "index_panic_candidate", ...})

        # Macro invocations of unimplemented!/todo!/unreachable!/panic!
        if n.type == "macro_invocation":
            macro_name = source[n.start_byte:n.end_byte].decode().split("!")[0].strip()
            if macro_name in ("unimplemented", "todo", "unreachable", "panic"):
                findings.append({"type": "panic_macro", "macro": macro_name, ...})
    return findings
```

Cross-reference with `data/panic_unwind_apis.json` for stdlib calls that
panic on certain inputs (e.g., `slice::get_unchecked`-vs-`[i]`).

---

### 3.5 `scan_pyclass_traits.py`

For each `#[pyclass]` struct, extract the field list and check field types
against `data/non_send_sync_types.json` (`#[pyclass]` requires `Send + Sync`
since PyO3 0.23 — flag fields breaking either bound).

```python
def analyze_pyclass(tree: Tree, source: bytes,
                   non_send: dict) -> list:
    findings = []
    for struct_node in extract_attribute_targets(tree, source, "pyclass"):
        attr_node = preceding_pyclass_attribute(struct_node, source)
        options = parse_pyclass_options(attr_node, source)
        # options is a dict like {"unsendable": False, "frozen": False, ...}

        # Get field types
        fields = extract_field_types(struct_node, source)

        if not options.get("unsendable"):
            for field in fields:
                if is_non_send_type(field["type"], non_send):
                    findings.append({
                        "type": "non_send_field_without_unsendable",
                        "struct": struct_node_name(struct_node, source),
                        "field": field["name"],
                        "field_type": field["type"],
                        ...
                    })
    return findings

def parse_pyclass_options(attr_node: Node, source: bytes) -> dict:
    # The attribute is #[pyclass(opt1, opt2, opt3 = val)]
    # Parse the token tree inside the parentheses.
    ...
```

Attribute-option parsing is fiddly. `tree-sitter-rust` gives you a
`token_tree` for attribute args; you walk its children and pick out
identifiers and key-value pairs. Don't try to be a full Rust parser —
recognize the option names you care about (`unsendable`, `frozen`,
`subclass`, `extends`, `dict`, `weakref`, `eq`, `ord`, `hash`).

---

### 3.6 `scan_pyclass_protocol.py`

For each `#[pyclass]` struct, find the matching `#[pymethods] impl Struct`
block, walk its methods, and check signatures.

```python
def check_pyclass_protocol(tree: Tree, source: bytes) -> list:
    findings = []
    pyclasses = extract_attribute_targets(tree, source, "pyclass")
    for struct_node in pyclasses:
        struct_name = struct_node_name(struct_node, source)
        fields = extract_field_types(struct_node, source)
        methods_impl = find_pymethods_impl(tree, source, struct_name)
        if methods_impl is None:
            continue

        method_attrs = extract_method_attributes(methods_impl, source)
        # method_attrs = {"new": method_node, "increment": method_node, ...}

        # Has Py<T> / Bound<'py, T> fields → needs __traverse__/__clear__
        if any(holds_py_handle(f["type"]) for f in fields):
            if "__traverse__" not in method_attrs:
                findings.append({"type": "missing_traverse", ...})
            if "__clear__" not in method_attrs:
                findings.append({"type": "missing_clear", ...})

        # Validate __richcmp__ signature
        if "__richcmp__" in method_attrs:
            check_richcmp_signature(method_attrs["__richcmp__"], source, findings)

        # Validate __hash__ signature: should return isize or u64
        if "__hash__" in method_attrs:
            check_hash_signature(method_attrs["__hash__"], source, findings)

        # Validate __new__ vs __init__
        if "__new__" in method_attrs and "__init__" in method_attrs:
            findings.append({"type": "new_and_init_collision", ...})

    return findings
```

`__richcmp__` semantics — the cext-review-toolkit lessons port directly:

```rust
fn __richcmp__(&self, other: &Self, op: CompareOp) -> PyResult<PyObject> {
    match op {
        CompareOp::Eq => Ok(self.value == other.value).into_py(...),
        // Missing: Ne, Lt, Le, Gt, Ge
        // Should fall through to py.NotImplemented().into() for unhandled ops
    }
}
```

Check that all 6 ops are handled OR there's a default arm returning
`py.NotImplemented().into()`. Detect "returning a borrowed reference":
look for `py.NotImplemented()` *not* followed by `.into()` /
`.into_py(py)` / similar (the borrowed-reference return is a bug per cext
type-slot-checker findings).

---

### 3.7 `scan_module_init.py`

Find the `#[pymodule]` function. Walk for `static OnceLock<Py<_>>` /
`static Lazy<Py<_>>` patterns at module scope. Walk the body for
`module.add_class::<T>()` / `module.add(...)` calls — flag those that
aren't `?`-shortcircuited.

```python
def check_module_init(tree: Tree, source: bytes) -> list:
    findings = []
    # Find #[pymodule] function
    pymod_fns = extract_attribute_targets(tree, source, "pymodule")
    if not pymod_fns:
        return findings  # no #[pymodule] in this file
    pymod_fn = pymod_fns[0]

    # Check static items at module scope
    for node in tree.root_node.children:
        if node.type != "static_item":
            continue
        ty = node.child_by_field_name("type")
        if ty is None:
            continue
        type_text = source[ty.start_byte:ty.end_byte].decode()
        if "Py<" in type_text and ("OnceLock" in type_text or "Lazy" in type_text):
            findings.append({"type": "module_level_py_static", ...})

    # Check add_class / add calls inside the body
    body = pymod_fn.child_by_field_name("body")
    registrars = {"add_class", "add", "add_function", "add_submodule"}
    for call in find_calls_in_scope(body, source):
        if call["kind"] != "method" or call["name"] not in registrars:
            continue
        # Is the call wrapped in `?` (try_expression as parent)?
        if call["node"].parent.type != "try_expression":
            findings.append(
                {"type": "module_add_unchecked", "method": call["name"], ...}
            )

    return findings
```

---

### 3.8 `scan_lifetime_handles.py`

Walk function signatures and let-bindings for the type patterns:

```python
def check_handle_kinds(tree: Tree, source: bytes,
                      pyo3_version: str) -> list:
    findings = []

    # Walk for &PyAny / &PyDict / &PyList in function signatures and types
    for node in walk(tree.root_node):
        if node.type == "reference_type":
            type_text = source[node.start_byte:node.end_byte].decode()
            for legacy in ("&PyAny", "&PyDict", "&PyList", "&PyTuple",
                           "&PyString", "&PyBytes", "&PyBool"):
                if legacy in type_text:
                    severity = severity_for_legacy(pyo3_version)
                    findings.append({
                        "type": "legacy_ref_type",
                        "pattern": legacy,
                        "severity": severity,
                        ...
                    })

        # Py<T> parameter that's immediately bound — heuristic, slow
        if node.type == "function_item":
            params = node.child_by_field_name("parameters")
            body = node.child_by_field_name("body")
            if params and body:
                py_params = find_py_t_params(params, source)
                for p in py_params:
                    if param_is_only_bound_once(p, body, source):
                        findings.append({
                            "type": "py_param_should_be_bound",
                            ...
                        })

    return findings
```

---

### 3.9 `scan_pyo3_version_compat.py`

Walk the AST against `data/pyo3_api_tables.json`. For each call/method/path
that maps to a versioned API, check whether the discovered PyO3 version is
within the API's range.

```python
def check_version_compat(tree: Tree, source: bytes,
                          pyo3_version: str,
                          api_table: dict) -> list:
    findings = []
    for node in walk(tree.root_node):
        if node.type not in ("call_expression", "scoped_identifier"):
            continue
        symbol = extract_symbol(node, source)
        api_info = api_table.get(symbol)
        if api_info is None:
            continue
        if version_is_in_removed_range(pyo3_version, api_info):
            findings.append({"type": "removed_api_use", ...})
        elif version_is_in_deprecated_range(pyo3_version, api_info):
            findings.append({"type": "deprecated_api_use", ...})
    return findings
```

`api_table` is keyed by symbol name or pattern (e.g., `".downcast"` →
`{"renamed_to": ".cast", "renamed_in": "0.27"}`).

---

### 3.10 `measure_rust_complexity.py`

Per design §3.2.10. Cyclomatic complexity = count of:

- `if` / `else if` (one per arm — `if` adds 1)
- `match_arm` (one per arm, beyond the first)
- `while`, `for`, `loop`
- `try_expression` (each `?` adds 1)
- `&&`, `||` (each adds 1)

Plus the function's own complexity score = 1 baseline. Use the same
formula as cext's `measure_c_complexity.py`:

```
score = (LOC / 100) + (cyclomatic / 10) + (nesting / 5) + (params / 7) + bonuses
```

Bonuses unique to Rust:

- `unsafe` blocks in body → +1 per block (an unsafe-heavy function is
  reviewed at higher priority)
- More than 3 `?` chains in a single statement → +0.5 (cognitive load)

Output the top-N by score plus full distribution stats.

---

### 3.11 `analyze_history.py`

Same shape as code-review-toolkit's version. Use `tree_sitter_rust` for
function-boundary detection. Classify commits by keyword presence in
message:

- `unsafe`, `soundness` → "safety"
- `unwrap`, `panic` → "panic-safety"
- `Send`, `Sync`, `frozen` → "concurrency"
- `Bound`, `Py::bind`, `clone_ref` → "migration"
- `fix`, `bug`, `crash` → "bugfix"

Use these to rank commits by "similar bug" risk when computing the
churn-risk matrix.

---

## 4. Common pitfalls

### 4.1 Attribute argument parsing

`#[pyclass(unsendable, frozen, extends = MyParent)]` — the args are a
`token_tree`. `tree-sitter-rust` doesn't parse them into structured args;
you get a sequence of tokens. Walk them yourself:

```python
def parse_attribute_args(attr_node: Node, source: bytes) -> dict:
    args = {}
    # Find the token_tree child of the attribute
    for child in attr_node.children:
        if child.type == "token_tree":
            # Walk its children, look for identifier or (identifier, =, value) patterns
            ...
    return args
```

There's no built-in helper. Write the walker once in `rust_ts_utils.py` and
reuse it.

### 4.2 Macros that look like calls

`wrap_pyfunction!(my_fn, m)` — this is a `macro_invocation`, not a call.
Don't confuse them. Filter by node type.

### 4.3 Lifetimes vs generics in `Bound<'py, T>`

`generic_type` has `type_arguments` containing both `lifetime` and `type`
nodes. To extract just the `'py`:

```python
def extract_lifetimes(type_args: Node) -> list[str]:
    return [
        source[c.start_byte:c.end_byte].decode()
        for c in type_args.children
        if c.type == "lifetime"
    ]
```

### 4.4 Method calls on chained expressions

`obj.call_method0("close").unwrap().getattr("foo")?` — five method calls
in one chain. Walking surfaces them in a particular order; if you're
counting unwraps, walk *all* of them, not just the outer call.

### 4.5 Comments contain misleading keywords

A comment like `// not really unsafe, just FFI` will trip a naive
"contains the word unsafe" check. **Always strip comments before pattern
matching** via the `strip_comments()` helper in `rust_ts_utils.py`. The
cext-review-toolkit ran into this exact bug in early Round 1 testing —
follow the lesson.

### 4.6 `use` statement aliasing

`use pyo3::ffi as raw_ffi;` — calls to `raw_ffi::Py_IncRef(...)` should
still be flagged. Build a `use` table per file:

```python
def build_use_table(tree: Tree, source: bytes) -> dict[str, str]:
    # Maps local name -> fully-qualified path
    # "raw_ffi" -> "pyo3::ffi"
    # "Bound" -> "pyo3::Bound" (from `use pyo3::Bound;`)
    # "*" -> "pyo3::prelude::*" (glob imports)
    ...
```

Resolve callees against this table before string-matching.

### 4.7 Tree-sitter parsing errors

Some Rust files have parse errors (Tree-sitter is approximate around very
new syntax or unusual macro use). Don't crash on `tree.root_node.has_error`:
log a warning, skip the file, continue.

### 4.8 `byte` vs `char` offsets

`node.start_byte` and `node.end_byte` index into the *byte* source. If
you decode to a Python `str` and try to slice with byte offsets, you'll
break on multi-byte characters. Always slice on bytes, decode last:

```python
text = source[node.start_byte:node.end_byte].decode("utf-8")
```

### 4.9 Long files

Tree-sitter is fast but allocating Python objects for every node visit is
not. For large files (`polars-python/src/lazyframe/mod.rs` is ~5000 lines),
prefer streaming walks with early-exit over collecting all-nodes lists.

---

## 5. Test approach

### 5.1 Use `TempRustExtension` for scanner integration tests

**Tests use `unittest`, never pytest** (per the design and CLAUDE.md). Every
test is a method on a `unittest.TestCase` subclass, using `self.assert*`:

```python
import unittest
from tests.helpers import TempRustExtension, import_script


class TestScanUnsafeBlocks(unittest.TestCase):
    def test_finds_transmute(self):
        scanner = import_script("scan_unsafe_blocks")
        fixture = {"src/lib.rs": FIXTURE_UNSAFE_TRANSMUTE}
        with TempRustExtension(fixture) as ext:
            result = scanner.analyze(str(ext.root))
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(
            result["findings"][0]["type"], "unsafe_transmute_between_handles"
        )
```

### 5.2 Unit tests for the utility functions

```python
class TestRustTsUtils(unittest.TestCase):
    def test_extract_unsafe_blocks_handles_nested(self):
        source = b"""\
fn a() {
    unsafe {
        unsafe {
            something();
        }
    }
}
"""
        tree = parse_bytes(source)
        blocks = extract_unsafe_blocks(tree.root_node, source)
        self.assertEqual(len(blocks), 2)  # both inner and outer
```

### 5.3 Negative tests are equally important

For every "true positive" test, write a "true negative" test that confirms
the scanner does *not* fire on similar-but-clean code:

```python
class TestScanUnsafeBlocksNegative(unittest.TestCase):
    def test_skips_safety_commented(self):
        # Same shape as FIXTURE_UNSAFE_FFI_CALL but with a // SAFETY: comment
        fixture = {"src/lib.rs": FIXTURE_UNSAFE_WITH_SAFETY}
        with TempRustExtension(fixture) as ext:
            result = import_script("scan_unsafe_blocks").analyze(str(ext.root))
        # Still reported (CONSIDER by default) but downgraded to ACCEPTABLE
        self.assertTrue(
            all(f["classification"] == "ACCEPTABLE" for f in result["findings"])
        )
```

### 5.4 Edge case tests

For each scanner, write at least one test for each of:

- Generic-parameter use (`fn foo<T: Send>(...)`)
- Heavy attribute use (`#[pyclass(unsendable, frozen, extends = X)]`)
- Macro-rich code (`wrap_pyfunction!`, `pyfunction_macro!`)
- Empty file / file with only comments
- File with parse errors (assert: scanner doesn't crash)

### 5.5 Version-aware tests

For `pyo3-version-compat-scanner` and `lifetime-handle-checker`:

```python
class TestLifetimeHandlesVersionAware(unittest.TestCase):
    def _legacy_findings(self, pyo3_version):
        fixture = {
            "Cargo.toml": cargo_toml_with(pyo3=pyo3_version),
            "src/lib.rs": FIXTURE_LEGACY_PYANY_PARAM,
        }
        with TempRustExtension(fixture) as ext:
            result = import_script("scan_lifetime_handles").analyze(str(ext.root))
        return [f for f in result["findings"] if f["type"] == "legacy_ref_type"]

    def test_pyo3_021_demotes_legacy_pyany_to_consider(self):
        findings = self._legacy_findings("0.21")
        self.assertTrue(all(f["classification"] == "CONSIDER" for f in findings))

    def test_pyo3_028_promotes_legacy_pyany_to_fix(self):
        findings = self._legacy_findings("0.28")
        self.assertTrue(all(f["classification"] == "FIX" for f in findings))
```

This pattern matters because version sensitivity is the whole point of
several scanners; without these tests, version regression goes unnoticed.

---

## 6. When tree-sitter is awkward, fall back to ts-queries

For some patterns, tree-sitter-rust's
[query language](https://tree-sitter.github.io/tree-sitter/using-parsers#pattern-matching-with-queries)
is more concise than Python walker code. Example: find all `unsafe` blocks
that contain a `transmute` call:

```scheme
(unsafe_block
  (block
    (expression_statement
      (call_expression
        function: (scoped_identifier
          path: (_)
          name: (identifier) @callee)
        (#match? @callee "transmute"))))) @unsafe-with-transmute
```

Run via `tree_sitter.Query(language, query_string)` and
`query.captures(root_node)`. Use this style when:

- The pattern is structural (parent-child relationship) rather than data-driven
- You don't need to look up against external data
- You want the query as a single textual asset (easier to maintain)

Avoid ts-queries when:

- You need to cross-reference against JSON data files mid-walk
- The patterns require multi-pass analysis (collect, then check)
- The pattern depends on the file's `use` table (queries can't see it)

The cext-review-toolkit didn't use ts-queries heavily (only walker code).
For Rust, ts-queries are slightly more useful because Rust's grammar is
more regular. Use them where they help; don't force them.

---

## 7. References

When stuck on a node-type question, the authoritative resources are:

- Node types: <https://github.com/tree-sitter/tree-sitter-rust/blob/master/src/node-types.json>
  (the json schema; use this to know what fields a node has)
- Grammar: <https://github.com/tree-sitter/tree-sitter-rust/blob/master/grammar.js>
  (the source; use this to understand what a node *means*)
- Playground: <https://tree-sitter.github.io/tree-sitter/playground>
  (paste Rust source, get a tree; invaluable for sanity-checking)

When in doubt about what tree-sitter-rust thinks a piece of source looks
like, paste it into the playground first. The grammar handles a lot of
edge cases you'd otherwise have to reverse-engineer.
