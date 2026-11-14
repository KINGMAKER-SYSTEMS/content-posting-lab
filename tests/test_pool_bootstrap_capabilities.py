"""Offline boundary tests of the complete capabilities function body.

AST loading avoids importing the application and its provider/environment
initializers. Inventory, cache and registry collaborators are deterministic
fixtures; the real committed pool-list loader and response serializer run.
These are function-boundary tests, not HTTP/auth, full app or render tests.
Run directly with Python; no pytest fixtures or temporary cleanup are needed.
"""
from __future__ import annotations
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pool_ids_under_test", ROOT / "services/pool_supplied_pages.py")
pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool)

def functions():
    path = Path(os.environ.get("CAPABILITY_SOURCE_FILE", ROOT / "routers/control_plane.py"))
    tree = ast.parse(path.read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "capabilities", "_capabilities_response", "_pool_supplied_recipe_without_library"
        }:
            node.decorator_list = []
            selected.append(node)
    assert len(selected) == 3
    return compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + selected, type_ignores=[])), str(path), "exec")

class BootstrapBoundary(unittest.TestCase):
    def call(self, page, engine="lyrics_slideshows", *, reasons=True,
             commissioned=True, current=True, registered=False):
        profile = NS(content_niche="fixture", content_engine=engine,
                     execution_status="commissioned" if commissioned else "incomplete",
                     format_slug="fixture-format", format_contract_version="fixture-v1", max_quantity=10)
        publication = dict(recipeId="registered:master", engine="lyrics_slideshows",
                           recipeVersion="registered-v2", production={"sourceLibraryId": None})
        g = dict(json=json, Header=lambda **kw: None,
                 PAGE_ID_RE=re.compile(r"^[A-Za-z0-9._:-]{1,128}$"),
                 _current_intent_for_capabilities=lambda _: ({"contentNiche":"fixture", "contentEngine":engine}, "intent-hash") if current else None,
                 _read_jobs_snapshot_object=lambda: NS(generation=1),
                 list_registered_recipe_bindings=lambda *args: [("registered",publication)] if registered else [],
                 load_engine_registry=lambda: ({"fixture":profile}, "registry-hash"),
                 _capabilities_binding_has_slideshow=lambda _: False,
                 _contracts_freshness_signature=lambda: (),
                 _capabilities_catalog_signature=lambda _: (),
                 _capabilities_binding_engines=lambda _: set(),
                 _capabilities_cache_key=lambda *args: "cache-key",
                 _capabilities_cache_lookup=lambda _: None,
                 _capabilities_cache_store=lambda *args: None,
                 _capabilities_job_views=lambda *args: ({"jobs":{}, "archive":{}}, {}),
                 compaction=NS(ARCHIVE_INDEX_KEY="archive"),
                 page_niche_admits=lambda niche, wanted, eng: niche == wanted,
                 _SLIDESHOW_ENGINES=frozenset({"sourced_slideshow", "lyrics_slideshows", "sourced_image"}),
                 is_pool_supplied=pool.is_pool_supplied,
                 typed_recipe_spec=lambda p: p,
                 POOL_SUPPLIED="pool_supplied", MAX_CAPABILITIES=100,
                 _env_flag=lambda name: reasons if name == "reasons" else False,
                 ADVERTISE_ZERO_REASONS_ENV="reasons", ADVERTISE_CONSTRAINTS_ENV="constraints",
                 WORKER_CAPABILITY_ENTRY_FIELDS={"recipeId","engine","recipeVersion","maxQuantity","sourceIdentities"},
                 RESPONSE_SCHEMA="content-lab.response.v1")
        exec(functions(), g)
        return g["capabilities"](x_rt_page_id=page, x_page_id=None)

    def test_every_listed_page_and_slideshow_engine_is_discoverable_at_zero(self):
        ids = pool.load_pool_supplied_page_ids()
        self.assertEqual(len(ids),17)
        for page in ids:
            for engine in ("sourced_slideshow","lyrics_slideshows","sourced_image"):
                with self.subTest(page=page,engine=engine):
                    self.assertEqual(self.call(page,engine)["capabilities"], [dict(
                        recipeId="fixture-format:master",engine=engine,
                        recipeVersion="fixture-v1",maxQuantity=0,zeroReason="pool_supplied")])

    def test_unlisted_and_near_aliases_keep_original_ceiling_and_shape(self):
        for page in ("acct:unlisted", "acct:chemically.lit", "acct:chemically_lit", "acct:chemically-lit-extra"):
            with self.subTest(page=page):
                self.assertEqual(self.call(page)["capabilities"], [dict(
                    recipeId="fixture-format:master",engine="lyrics_slideshows",
                    recipeVersion="fixture-v1",maxQuantity=10)])

    def test_other_engines_on_listed_page_keep_original_ceiling(self):
        for engine in ("ai_video", "sourced_video"):
            self.assertEqual(self.call("acct:chemically-lit",engine)["capabilities"][0]["maxQuantity"],10)

    def test_feature_flag_off_keeps_worker_closed_shape(self):
        row=self.call("acct:chemically-lit",reasons=False)["capabilities"][0]
        self.assertEqual(row,dict(recipeId="fixture-format:master",engine="lyrics_slideshows",recipeVersion="fixture-v1",maxQuantity=0))

    def test_registered_publication_version_remains_authoritative(self):
        row=self.call("acct:chemically-lit",registered=True)["capabilities"]
        self.assertEqual(row,[dict(recipeId="registered:master",engine="lyrics_slideshows",recipeVersion="registered-v2",maxQuantity=0,zeroReason="pool_supplied")])

    def test_missing_intent_or_uncommissioned_profile_stays_empty(self):
        self.assertEqual(self.call("acct:chemically-lit",current=False)["capabilities"],[])
        self.assertEqual(self.call("acct:chemically-lit",commissioned=False)["capabilities"],[])

if __name__ == "__main__":
    unittest.main(verbosity=2)
