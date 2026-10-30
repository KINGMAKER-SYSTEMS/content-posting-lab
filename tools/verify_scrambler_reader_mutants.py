"""Replay compiling Scrambler reader mutants against actual behavioral tests.

Run remotely through suite-slot with MAJ_CLASS=heavy. Baseline must pass first;
logs, diffs, source hash and results stay in the printed evidence directory.
No application file is modified. A broken/missing mutation needle fails closed.
"""
import difflib, hashlib, json, os, re, subprocess, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=Path(os.environ.get("SCRAMBLER_MUTANT_OUT") or tempfile.mkdtemp(prefix="scrambler-reader-mutants-"));out.mkdir(parents=True,exist_ok=True)
print("Evidence directory:", out, flush=True)
source=(root/"services/scrambler_originality.py").read_text()
(out/"source.sha256").write_text(hashlib.sha256(source.encode()).hexdigest()+"\n")
(out/"bootstrap.py").write_text("""import importlib.util,sys,pytest
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
import services
spec=importlib.util.spec_from_file_location("services.scrambler_originality",sys.argv[2])
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;services.scrambler_originality=m;spec.loader.exec_module(m)
files=["test_scrambler_originality_contract_independent.py","test_scrambler_originality_boundaries.py","test_scrambler_posted_asset_evidence.py","test_scrambler_reader_review_regressions.py"]
raise SystemExit(pytest.main([*[str(root/"tests"/p) for p in files],"-q","--tb=short",*sys.argv[3:]]))
""")
cases=[('same-page-cap', 'SAME_PAGE_MAX_SHOWS = 2', 'SAME_PAGE_MAX_SHOWS = 3', 'third_same_page'),
 ('seven-day-gap',
  'SAME_PAGE_GAP_S = 7 * 86_400.0',
  'SAME_PAGE_GAP_S = 0.0',
  'future_own_page_show'),
 ('cross-page-90d',
  'CROSS_PAGE_WINDOW_S = 90 * 86_400.0',
  'CROSS_PAGE_WINDOW_S = 0.0',
  'recent_show_through_another_master'),
 ('retired-alias',
  'if (mapping.origin_kind, mapping.origin_id) in self.retired_origins:',
  'if False:',
  'retired_origin_alias'),
 ('case-sensitive-origin',
  '_OriginWindow(origin_kind=kind, origin_id=origin_id,',
  '_OriginWindow(origin_kind=kind, origin_id=origin_id.lower(),',
  'case_sensitive_origin_identity'),
 ('wildcard-prior',
  'for show in self.wildcard_shows.get((mapping.origin_kind, mapping.origin_id), ()):',
  'for show in ():',
  'unknown_prior_window'),
 ('partial-origin-second',
  'last = int(math.ceil(end_s)) - 1',
  'last = int(math.floor(max(start_s, end_s - 0.001)))',
  'partial_millisecond'),
 ('aggregate-entry-budget',
  'if entries > USAGE_MAX_INDEX_ENTRIES:',
  'if False:',
  'index_entry_budget_counts'),
 ('upstream-offset',
  'origin_start = int(offset_ms) + start_ms',
  'origin_start = start_ms',
  'canonical_origin_window_applies_upstream_offset'),
 ('mapped-bounds',
  'if start_ms < Decimal(str(mapping.window[0])) * 1000 or end_ms > '
  'Decimal(str(mapping.window[1])) * 1000:',
  'if False:',
  'candidate_must_have_positive_window'),
 ('future-own-age',
  'if own_age < SAME_PAGE_WINDOW_S:',
  'if 0 <= age < SAME_PAGE_WINDOW_S:',
  'future_own_page'),
 ('sha-hex-validity',
  'if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):',
  'if len(text) != 64:',
  'nonhex_retirement'),
 ('ownership-namespace',
  'mine = frozenset(normalize_handle(h) for h in page_handles)',
  'mine = frozenset(normalize_handle(h) for h in page_handles) | {page_id}',
  'implicit_handle_alias'),
 ('decimal-milliseconds',
  'offset_ms = Decimal(str(mapping.window[0])) * 1000',
  'offset_ms = mapping.window[0] * 1000.0',
  'decimal_millisecond_mapping'),
 ('finite-clock', 'if _finite_number(now) is None:', 'if False:', 'nonfinite'),
 ('normalized-master-collision',
  'if master in seen_masters:',
  'if False:',
  'conflicting_normalized'),
 ('posted-full-coverage',
  'covered = record.first_second <= first and record.last_second >= last',
  'covered = True',
  'partial_coverage_refuses'),
 ('posted-asset-binding',
  'self.posted_assets.get((asset, origin_kind, origin_id), ())',
  'next(iter(self.posted_assets.values()), ())',
  'wrong_or_unknown_asset or origin_identity_matches_case_sensitively'),
 ('posted-latest',
  'record.posted_epoch >= best.posted_epoch',
  'record.posted_epoch <= best.posted_epoch',
  'latest_of_multiple_covering'),
 ('posted-metadata-budget',
  'if entries > USAGE_MAX_INDEX_ENTRIES:',
  'if False:',
  'metadata_records_count')]
results=[]
def run(name,body,selector=None):
 p=out/(name+".py");p.write_text(body);compile(body,str(p),"exec")
 cmd=[sys.executable,str(out/"bootstrap.py"),str(root),str(p)]
 if selector:cmd+=["-k",selector]
 r=subprocess.run(cmd,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
 (out/(name+".log")).write_text(r.stdout)
 return r
base=run("base",source)
results.append({"case":"base","exit":base.returncode,"passed":base.returncode==0})
if base.returncode: print(base.stdout);raise SystemExit("base failed; no mutant claims")
for name,old,new,selector in cases:
 assert old in source,(name,"needle missing")
 mutant=source.replace(old,new)
 (out/(name+".diff")).write_text("".join(difflib.unified_diff(source.splitlines(True),mutant.splitlines(True),fromfile="services/scrambler_originality.py",tofile=name+".py")))
 r=run(name,mutant,selector)
 red=r.returncode==1 and (bool(re.search(r"^E\s+(?:assert\b|AssertionError|Failed: DID NOT RAISE)",r.stdout,re.M)) or "DID NOT RAISE" in r.stdout) and "ERROR collecting" not in r.stdout and "ImportError" not in r.stdout and "SyntaxError" not in r.stdout
 row={"case":name,"exit":r.returncode,"behaviorally_red":red}
 results.append(row);print(json.dumps(row),flush=True)
(out/"results.json").write_text(json.dumps(results,indent=2)+"\n")
raise SystemExit(0 if all(r.get("passed",r.get("behaviorally_red",False)) for r in results) else 1)
