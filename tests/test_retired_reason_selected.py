"""Stdlib selected-function contract; not HTTP/auth/startup coverage.

Run only through the approved remote gate. No application module is imported.
Dependencies are explicit inert fixtures; actual resolver, capability caller,
cache response and flag projection bodies are selected mechanically by AST.
"""
from __future__ import annotations
import ast
import builtins
import copy
import dataclasses
import json
import logging
import os
from pathlib import Path
import re
import sys
import types
import unittest

ROOT = Path(os.environ.get('LAB_REASON_SOURCE', Path(__file__).resolve().parents[1]))


def selected(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(), filename=path)
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise AssertionError(f'missing selected definitions: {names}')
    for node in nodes:
        # Only framework registration decorators are omitted. Bodies unchanged.
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, 'exec'), namespace)


class SelectedRetiredReason(unittest.TestCase):
    def setUp(self):
        self.original_env = {name: os.environ.get(name) for name in (
            'CONTENT_LAB_ADVERTISE_ZERO_REASONS', 'CONTENT_LAB_ADVERTISE_SUPPORTED_CONSTRAINTS')}
        os.environ['CONTENT_LAB_ADVERTISE_ZERO_REASONS'] = '1'
        os.environ['CONTENT_LAB_ADVERTISE_SUPPORTED_CONSTRAINTS'] = '0'
        self.calls = []
        self.cache = None
        self.publication = {'pageId': 'page-1', 'recipeId': 'dirt:master', 'engine': 'sourced_video', 'recipeVersion': 'v1'}
        self.spec = {'schema': 'dossier.recipe-spec.v4', 'masterPages': {'pageId': 'page-1'},
                     'masterPagesHash': 'mh', 'production': {'sourceLibraryId': 'lib-1', 'catalogVersion': 'catalog'}}
        self.profile = types.SimpleNamespace(content_engine='sourced_video', material_source='source_library',
            executor_kind='source_dna_recut', executor_id='exec', executor_version='sha256:eh', format_slug='dirt',
            max_quantity=2, registry_hash='rh', authority_hash='ah', format_contract_version='fv', asset_type='video')
        self.master = types.SimpleNamespace(sha256='retired', provenance={'sourceUrl': 'https://example.test/source'})
        self.library = types.SimpleNamespace(library_id='lib-1', page_id='page-1', format_slug='dirt', masters=(self.master,), sha256='lh')
        class SourceDnaError(Exception): pass
        class ShipStreamSourceError(Exception): pass
        self.SourceDnaError = SourceDnaError
        self.ShipStreamSourceError = ShipStreamSourceError
        ingredients = types.SimpleNamespace(is_pinned_legacy_catalog_version=lambda *a, **k: False,
            selected_dossier_catalog_version=lambda *a, **k: 'catalog')
        def only_fixture_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == '__future__' and not level:
                return builtins.__import__(name, globals, locals, fromlist, level)
            if name != 'services.dossier_ingredients' or level:
                raise AssertionError(f'unexpected selected-code import {name}')
            return ingredients
        self.ns = {'__name__': 'selected_retired_reason', '__builtins__': dict(vars(builtins), __import__=only_fixture_import),
            'Header': lambda **kwargs: None, '_registered_recipe': lambda *a: None,
            'dataclass': dataclasses.dataclass, 'json': json, 'os': os, 'log': logging.getLogger('private.retired'),
            'SourceDnaError': SourceDnaError, 'ShipStreamSourceError': ShipStreamSourceError,
            'SOURCE_MASTER_RETIRED': 'source_master_retired', 'RETIRED_MASTER_SHAS': frozenset({'retired'}),
            'typed_recipe_spec': lambda publication: self.spec,
            'resolve_material_profile': lambda *a: self.profile,
            '_executor_contract': lambda: ({'executorId': 'exec', 'output': {'width': 1080, 'height': 1920, 'encodePreset': 'p'}}, 'eh'),
            'load_source_dna_library': self.load_library,
            'load_shipstream_source_dna_library': self.load_shipstream,
            'load_format_contracts': lambda: ({'dirt': {}}, 'ch'),
            '_cut_duration': lambda *a: 6000, '_minimum_source_start': lambda *a: 0}
        module = types.ModuleType(self.ns['__name__']); module.__dict__.update(self.ns)
        self.ns = module.__dict__; self.module_name = module.__name__
        sys.modules[self.module_name] = module
        source_tree = ast.parse((ROOT/'services/control_plane_sources.py').read_text())
        names = {'SourceRecipe', 'resolve_source_recipe'}
        if any(isinstance(n, ast.FunctionDef) and n.name == 'resolve_source_recipe_with_reason' for n in source_tree.body):
            names.add('resolve_source_recipe_with_reason')
        selected('services/control_plane_sources.py', names, self.ns)
        selected('routers/control_plane.py', {'_dossier_source_recipe', '_env_flag', '_zero_plan_recipe', '_capabilities_response', 'capabilities'}, self.ns)
        self.ns.update({
            'ADVERTISE_ZERO_REASONS_ENV': 'CONTENT_LAB_ADVERTISE_ZERO_REASONS',
            'ADVERTISE_CONSTRAINTS_ENV': 'CONTENT_LAB_ADVERTISE_SUPPORTED_CONSTRAINTS',
            'WORKER_CAPABILITY_ENTRY_FIELDS': frozenset({'recipeId','engine','recipeVersion','maxQuantity','sourceIdentities'}),
            'RESPONSE_SCHEMA': 'content-lab.capabilities.v1', 'SUPPORTED_CONSTRAINTS': ('priority',),
            'PAGE_ID_RE': re.compile(r'^[a-z0-9-]+$'), 'MAX_CAPABILITIES': 200,
            '_current_intent_for_capabilities': lambda page: ({'contentEngine': 'sourced_video'}, 'mh') if page=='page-1' else None,
            '_read_jobs_snapshot_object': lambda: types.SimpleNamespace(generation='g'),
            '_JobsDecodeFailed': type('DecodeError', (Exception,), {}),
            'list_registered_recipe_bindings': lambda *a: [('binding', self.publication)],
            'load_engine_registry': lambda: ({}, 'rh'),
            '_capabilities_binding_has_slideshow': lambda rows: False,
            '_contracts_freshness_signature': lambda: (), '_capabilities_catalog_signature': lambda rows: (),
            '_capabilities_binding_engines': lambda rows: (), '_capabilities_cache_key': lambda *a: 'private-key',
            '_capabilities_cache_lookup': lambda key: self.cache, '_capabilities_cache_store': self.store_cache,
            '_capabilities_job_views': lambda *a: ({'jobs': {}, 'archive': {}}, {}),
            'compaction': types.SimpleNamespace(ARCHIVE_INDEX_KEY='archive'),
            'canonical_source_identity': lambda value: value,
            '_SLIDESHOW_ENGINES': frozenset({'sourced_slideshow'}),
            'syzygy_runtime_available': lambda: True,
            'RECIPE_UNRESOLVED': 'recipe_unresolved', 'SLIDESHOW_RUNTIME_UNAVAILABLE': 'slideshow_runtime_unavailable',
            'SOURCE_IDENTITY_MISSING': 'source_identity_missing',
            '_source_dna_cut_ledger': lambda *a: (set(), {}),
            'plan_source_cuts': lambda *a, **k: [{'cut': 1}, {'cut': 2}],
            'explain_empty_source_plan': lambda *a: 'source_windows_exhausted',
            'HTTPException': type('PrivateHTTPException', (Exception,), {'__init__': lambda self, **kwargs: Exception.__init__(self, repr(kwargs))}),
        })

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        sys.modules.pop(self.module_name, None)

    def load_library(self, library_id):
        self.calls.append(('library', library_id))
        if library_id != self.library.library_id: raise self.SourceDnaError('private missing library')
        return self.library

    def load_shipstream(self, *args, **kwargs):
        self.calls.append(('shipstream', kwargs.get('expected_library_id')))
        raise self.ShipStreamSourceError('private unavailable')

    def store_cache(self, key, entries, rows):
        self.cache = (copy.deepcopy(entries), copy.deepcopy(rows))

    def answer(self):
        return self.ns['capabilities']('page-1', None)

    def expected_empty(self, reason=None):
        result = {'schema': 'content-lab.capabilities.v1', 'capabilities': []}
        if reason:
            result['zeroPlanRecipes'] = [dict(zeroReason=reason, recipeId='dirt:master', engine='sourced_video', recipeVersion='v1')]
        return result

    def test_retired_named_reason_through_actual_caller(self):
        self.assertEqual(self.answer(), self.expected_empty('source_master_retired'))
        self.assertEqual(self.calls, [('library', 'lib-1')])

    def test_retired_execution_wrapper_still_refuses(self):
        self.assertIsNone(self.ns['resolve_source_recipe'](self.publication))
        self.assertEqual(self.calls, [('library', 'lib-1')])

    def test_flag_off_preserves_wire_shape(self):
        os.environ['CONTENT_LAB_ADVERTISE_ZERO_REASONS'] = '0'
        self.assertEqual(self.answer(), self.expected_empty())

    def test_flag_toggle_cached_diagnostic_projects_only_when_enabled(self):
        os.environ['CONTENT_LAB_ADVERTISE_ZERO_REASONS'] = '0'
        self.assertEqual(self.answer(), self.expected_empty())
        os.environ['CONTENT_LAB_ADVERTISE_ZERO_REASONS'] = '1'
        self.assertEqual(self.answer(), self.expected_empty('source_master_retired'))
        self.assertEqual(self.calls, [('library', 'lib-1')])
        os.environ['CONTENT_LAB_ADVERTISE_ZERO_REASONS'] = '0'
        self.assertEqual(self.answer(), self.expected_empty())
        self.assertEqual(self.calls, [('library', 'lib-1')])

    def test_mixed_bindings_keep_reasons_request_local(self):
        publications = [dict(self.publication, recipeVersion=version) for version in ('retired','generic','healthy')]
        specs = {version: copy.deepcopy(self.spec) for version in ('retired','generic','healthy')}
        specs['generic']['schema'] = 'invalid'
        specs['healthy']['production']['sourceLibraryId'] = 'healthy-lib'
        healthy = copy.deepcopy(self.library)
        healthy.library_id = 'healthy-lib'; healthy.masters[0].sha256 = 'healthy'
        observed = []
        def typed(publication):
            observed.append(publication['recipeVersion'])
            return specs[publication['recipeVersion']]
        def load(library_id):
            self.calls.append(('library',library_id))
            if library_id == 'lib-1': return self.library
            if library_id == 'healthy-lib': return healthy
            raise AssertionError('unexpected library')
        self.ns['typed_recipe_spec'] = typed
        self.ns['load_source_dna_library'] = load
        self.ns['list_registered_recipe_bindings'] = lambda *a: list(enumerate(publications))
        self.assertEqual(self.answer(), {
            'schema':'content-lab.capabilities.v1',
            'capabilities':[{'recipeId':'dirt:master','engine':'sourced_video','recipeVersion':'healthy',
                             'maxQuantity':2,'sourceIdentities':['https://example.test/source']}],
            'zeroPlanRecipes':[
                {'recipeId':'dirt:master','engine':'sourced_video','recipeVersion':'retired','zeroReason':'source_master_retired'},
                {'recipeId':'dirt:master','engine':'sourced_video','recipeVersion':'generic','zeroReason':'recipe_unresolved'},
            ],
        })
        self.assertEqual(observed, ['retired','generic','healthy'])
        self.assertEqual(self.calls, [('library','lib-1'),('library','healthy-lib')])

    def test_healthy_source_plans_preserved(self):
        self.master.sha256 = 'healthy'
        response = self.answer()
        self.assertEqual(response, {'schema':'content-lab.capabilities.v1', 'capabilities':[
            {'recipeId':'dirt:master','engine':'sourced_video','recipeVersion':'v1','maxQuantity':2,
             'sourceIdentities':['https://example.test/source']} ]})
        self.assertEqual(self.calls, [('library','lib-1')])

    def test_unrelated_bad_spec_remains_generic(self):
        self.spec['schema'] = 'invalid'
        self.assertEqual(self.answer(), self.expected_empty('recipe_unresolved'))
        self.assertEqual(self.calls, [])

    def test_wrong_executor_remains_generic(self):
        self.profile.executor_version = 'wrong'
        self.assertEqual(self.answer(), self.expected_empty('recipe_unresolved'))
        self.assertEqual(self.calls, [])

    def test_missing_library_remains_generic(self):
        self.spec['production']['sourceLibraryId'] = 'missing'
        self.assertEqual(self.answer(), self.expected_empty('recipe_unresolved'))
        self.assertEqual(self.calls, [('library','missing'),('shipstream','missing')])

    def test_wrong_page_library_healthy_is_refused(self):
        self.master.sha256 = 'healthy'; self.library.page_id = 'other-page'
        self.assertEqual(self.answer(), self.expected_empty('recipe_unresolved'))

    def test_wrong_format_library_healthy_is_refused(self):
        self.master.sha256 = 'healthy'; self.library.format_slug = 'other-format'
        self.assertEqual(self.answer(), self.expected_empty('recipe_unresolved'))

    def test_unknown_page_has_no_binding_or_reason(self):
        self.assertEqual(self.ns['capabilities']('other-page',None), self.expected_empty())
        self.assertEqual(self.calls, [])

    def test_non_source_engine_never_calls_source_resolver(self):
        self.publication['engine'] = 'unknown'
        expected = self.expected_empty('recipe_unresolved'); expected['zeroPlanRecipes'][0]['engine']='unknown'
        self.assertEqual(self.answer(), expected)
        self.assertEqual(self.calls, [])

    def test_retirement_set_is_actual_enforcement_input(self):
        self.ns['RETIRED_MASTER_SHAS'] = frozenset()
        self.assertEqual(self.answer()['capabilities'][0]['maxQuantity'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
