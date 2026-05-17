"""Tests for rust_ts_utils — the tree-sitter-rust parsing core."""

import unittest

from fixtures import (
    MINIMAL_PYO3_CRATE,
    PYO3_CRATE_WITH_BUGS,
    PYO3_CRATE_WITH_PYCLASS,
    PYO3_CRATE_WITH_UNSAFE,
)
from helpers import TempRustExtension, import_script

rtu = import_script("rust_ts_utils")


def tree_and_src(code: str):
    src = code.encode("utf-8")
    return rtu.parse_bytes(src), src


class TestParsing(unittest.TestCase):
    def test_parses_clean_code(self):
        tree, _ = tree_and_src(MINIMAL_PYO3_CRATE)
        self.assertFalse(rtu.has_parse_errors(tree))
        self.assertEqual(tree.root_node.type, "source_file")

    def test_detects_parse_errors(self):
        tree, _ = tree_and_src("fn broken( {")
        self.assertTrue(rtu.has_parse_errors(tree))


class TestFnItems(unittest.TestCase):
    def test_extracts_free_functions(self):
        tree, src = tree_and_src(MINIMAL_PYO3_CRATE)
        fns = rtu.extract_fn_items(tree, src)
        names = {f["name"] for f in fns}
        self.assertEqual(names, {"add", "minimal"})
        add = next(f for f in fns if f["name"] == "add")
        self.assertEqual(add["return_type"], "i64")
        self.assertFalse(add["is_unsafe"])

    def test_extracts_methods_inside_impls(self):
        tree, src = tree_and_src(PYO3_CRATE_WITH_PYCLASS)
        names = {f["name"] for f in rtu.extract_fn_items(tree, src)}
        self.assertEqual(names, {"new", "increment", "value", "classy"})

    def test_detects_unsafe_and_extern_modifiers(self):
        tree, src = tree_and_src(
            'unsafe fn danger() {}\nextern "C" fn cb() {}\nfn plain() {}\n'
        )
        fns = {f["name"]: f for f in rtu.extract_fn_items(tree, src)}
        self.assertTrue(fns["danger"]["is_unsafe"])
        self.assertFalse(fns["plain"]["is_unsafe"])
        self.assertEqual(fns["cb"]["extern_abi"], "C")


class TestUnsafeBlocks(unittest.TestCase):
    def test_block_and_fn(self):
        tree, src = tree_and_src("fn a() { unsafe { foo(); } }\nunsafe fn b() {}\n")
        blocks = rtu.extract_unsafe_blocks(tree, src)
        kinds = sorted(b["kind"] for b in blocks)
        self.assertEqual(kinds, ["block", "fn"])
        block = next(b for b in blocks if b["kind"] == "block")
        self.assertEqual(block["function"], "a")

    def test_nested_unsafe_blocks_both_found(self):
        tree, src = tree_and_src("fn a() { unsafe { unsafe { x(); } } }")
        blocks = rtu.extract_unsafe_blocks(tree, src)
        self.assertEqual(sum(b["kind"] == "block" for b in blocks), 2)

    def test_unsafe_fixture(self):
        tree, src = tree_and_src(PYO3_CRATE_WITH_UNSAFE)
        blocks = rtu.extract_unsafe_blocks(tree, src)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["function"], "refcount_of")


class TestImplBlocks(unittest.TestCase):
    def test_inherent_and_trait_impls(self):
        tree, src = tree_and_src(
            "struct Foo;\nimpl Foo {}\nunsafe impl Send for Foo {}\n"
        )
        impls = rtu.extract_impl_blocks(tree, src)
        self.assertEqual(len(impls), 2)
        inherent = next(i for i in impls if i["trait"] is None)
        self.assertEqual(inherent["type"], "Foo")
        self.assertFalse(inherent["is_unsafe"])
        send = next(i for i in impls if i["trait"] == "Send")
        self.assertEqual(send["type"], "Foo")
        self.assertTrue(send["is_unsafe"])


class TestStructDefs(unittest.TestCase):
    def test_named_fields(self):
        tree, src = tree_and_src("struct Counter { value: i64, handle: Py<PyAny> }")
        structs = rtu.extract_struct_defs(tree, src)
        self.assertEqual(len(structs), 1)
        fields = {f["name"]: f["type"] for f in structs[0]["fields"]}
        self.assertEqual(fields, {"value": "i64", "handle": "Py<PyAny>"})

    def test_pyclass_fixture(self):
        tree, src = tree_and_src(PYO3_CRATE_WITH_PYCLASS)
        structs = rtu.extract_struct_defs(tree, src)
        self.assertEqual([s["name"] for s in structs], ["Counter"])


class TestAttributes(unittest.TestCase):
    def test_attribute_targets(self):
        tree, src = tree_and_src(PYO3_CRATE_WITH_PYCLASS)
        pyclasses = rtu.extract_attribute_targets(tree, src, "pyclass")
        self.assertEqual(len(pyclasses), 1)
        self.assertEqual(pyclasses[0]["target_type"], "struct_item")
        pymods = rtu.extract_attribute_targets(tree, src, "pymodule")
        self.assertEqual(pymods[0]["target_type"], "function_item")

    def test_attribute_args_parsed(self):
        tree, src = tree_and_src('#[pyclass(frozen, name = "Ctr")]\nstruct C;')
        hits = rtu.extract_attribute_targets(tree, src, "pyclass")
        self.assertIn("frozen", hits[0]["attr"]["args_text"])

    def test_extract_attributes_on_item(self):
        tree, src = tree_and_src("#[new]\n#[pyo3(signature = ())]\nfn f() {}")
        fn = next(rtu.walk(tree, "function_item"))
        attrs = rtu.extract_attributes(fn, src)
        self.assertEqual([a["name"] for a in attrs], ["new", "pyo3"])


class TestCalls(unittest.TestCase):
    def test_function_path_and_method_calls(self):
        tree, src = tree_and_src(
            "fn f() { compute(); raw::Py_None(); obj.call_method0(); }"
        )
        calls = {c["name"]: c for c in rtu.find_calls_in_scope(tree, src)}
        self.assertEqual(calls["compute"]["kind"], "function")
        self.assertEqual(calls["Py_None"]["kind"], "path")
        self.assertEqual(calls["Py_None"]["full_callee"], "raw::Py_None")
        self.assertEqual(calls["call_method0"]["kind"], "method")
        self.assertEqual(calls["call_method0"]["receiver_text"], "obj")

    def test_chained_method_calls(self):
        tree, src = tree_and_src('fn f() { obj.getattr("x").unwrap(); }')
        names = sorted(c["name"] for c in rtu.find_calls_in_scope(tree, src))
        self.assertEqual(names, ["getattr", "unwrap"])

    def test_fn_names_filter(self):
        tree, src = tree_and_src("fn f() { a(); b(); c(); }")
        calls = rtu.find_calls_in_scope(tree, src, fn_names={"b"})
        self.assertEqual([c["name"] for c in calls], ["b"])


class TestClosures(unittest.TestCase):
    def test_callee_filtered_closures(self):
        tree, src = tree_and_src(
            "fn f() { py.detach(|| compute()); other(|| noise()); }"
        )
        detach = rtu.find_closure_bodies(tree, src, callee="detach")
        self.assertEqual(len(detach), 1)
        self.assertEqual(detach[0]["callee"], "detach")
        all_closures = rtu.find_closure_bodies(tree, src)
        self.assertEqual(len(all_closures), 2)


class TestUseStatements(unittest.TestCase):
    def test_glob_alias_and_plain(self):
        tree, src = tree_and_src(
            "use pyo3::prelude::*;\nuse pyo3::ffi as raw;\nuse std::rc::Rc;\n"
        )
        uses = {u["path"]: u for u in rtu.extract_use_statements(tree, src)}
        self.assertTrue(uses["pyo3::prelude"]["is_glob"])
        self.assertEqual(uses["pyo3::ffi"]["alias"], "raw")
        self.assertEqual(uses["std::rc::Rc"]["names"], ["Rc"])


class TestStripComments(unittest.TestCase):
    def test_keyword_in_comment_is_blanked(self):
        src = b"fn f() { let x = 1; /* unsafe */ ok(); } // unwrap here"
        stripped = rtu.strip_comments(src)
        self.assertEqual(len(stripped), len(src))  # offsets preserved
        self.assertNotIn(b"unsafe", stripped)
        self.assertNotIn(b"unwrap", stripped)
        self.assertIn(b"ok()", stripped)  # real code untouched

    def test_newlines_preserved(self):
        src = b"fn f() {}\n// a comment\nfn g() {}\n"
        self.assertEqual(rtu.strip_comments(src).count(b"\n"), src.count(b"\n"))


class TestHelpers(unittest.TestCase):
    def test_temp_rust_extension(self):
        with TempRustExtension({"src/lib.rs": "fn main() {}"}) as ext:
            self.assertTrue((ext.root / "src" / "lib.rs").is_file())
            cargo = (ext.root / "Cargo.toml").read_text()
            self.assertIn("pyo3", cargo)
            self.assertIn('crate-type = ["cdylib"]', cargo)
            saved_root = ext.root
        self.assertFalse(saved_root.exists())

    def test_bugs_fixture_parses_clean(self):
        tree, _ = tree_and_src(PYO3_CRATE_WITH_BUGS)
        self.assertFalse(rtu.has_parse_errors(tree))


if __name__ == "__main__":
    unittest.main()
