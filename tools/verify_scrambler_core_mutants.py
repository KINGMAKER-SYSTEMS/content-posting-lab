"""Replay compiling Scrambler core mutants against actual behavioral tests.

Run remotely through suite-slot with MAJ_CLASS=heavy. Baseline must pass first;
logs, diffs, source hash and results stay in the printed evidence directory.
No application file is modified. A broken/missing mutation needle fails closed.
"""
import difflib, hashlib, json, os, re, subprocess, sys, tempfile, shutil
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=Path(os.environ.get("SCRAMBLER_MUTANT_OUT") or tempfile.mkdtemp(prefix="scrambler-core-mutants-"));out.mkdir(parents=True,exist_ok=True)
print("Evidence directory:", out, flush=True)
# These pure state tests use only stdlib + pytest and do not use app fixtures.
# Copy the exact source bytes into owned evidence so unrelated application
# conftest imports cannot prevent portable core verification on review hosts.
core_tests=out/"core-tests";core_tests.mkdir(exist_ok=True)
for name in ("state_contract_independent", "rotation_contract", "reverse_render_contract", "release_contract_independent"):
 shutil.copyfile(root/"tests"/("test_scrambler_"+name+".py"),core_tests/("test_scrambler_"+name+".py"))
source=(root/"services/scrambler.py").read_text()
(out/"source.sha256").write_text(hashlib.sha256(source.encode()).hexdigest()+"\n")
(out/"bootstrap.py").write_text("""import importlib.util,sys,pytest
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
testroot=Path(__file__).parent/"core-tests"
import services
spec=importlib.util.spec_from_file_location("services.scrambler",sys.argv[2])
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;services.scrambler=m;spec.loader.exec_module(m)
files=["test_scrambler_state_contract_independent.py","test_scrambler_rotation_contract.py","test_scrambler_reverse_render_contract.py","test_scrambler_release_contract_independent.py"]
selector=""
if len(sys.argv)>3:selector=sys.argv[3]
raise SystemExit(pytest.main([*[str(testroot/p) for p in files],"-q","--tb=short","--confcutdir="+str(testroot),"-k",selector]))
""")
cases=[
("fast-share",[("DEFAULT_FAST_SHARE = 0.0","DEFAULT_FAST_SHARE = 0.2")],"default_fast_share"),
("max-speed",[("FAST_SPEEDS = (1.1, 1.2, 1.3)","FAST_SPEEDS = (1.1, 1.2, 1.3, 1.4)")],"persisted_retired_speed"),
("colon-page",[('rpartition(":")','partition(":")')],"colon_page_id"),
("duplicate-cuts",[("if sorted(indices) != list(range(len(indices))):","if False:")],"duplicate_cut0"),
("partial-enrichment",[("if missing:\n","if False:\n")],"partial_enrichment"),
("rendered-cleanup",[('row.get("jobId") == job_id and row.get("state") == RESERVED','row.get("jobId") == job_id and row.get("state") in (RESERVED, RENDERED)')],"release_keeps_rendered"),
("daily-report-render-order",[('accepted.sort(key=lambda item: item[:3])', 'pass')],"daily_report_uses_actual_render_order"),
("future-report",[("if age < 0 or age > window_s:","if age > window_s:")],"report_counts_rendered"),
("lost-history",[("if marker.exists():","if False:")],"known_created_history_loss"),
("replay-config",[("existing = _draws_from_job(","fast_share_for(page_id)\n        existing = _draws_from_job(")],"invalid_new_config_keeps_existing"),
("off-grid-speed",[("speed not in SPEEDS","speed_bucket(speed) not in SPEEDS")],"persisted_off_grid"),
("worker-window-min",[("WORKER_SOURCE_WINDOW_MIN_MS = 5_000","WORKER_SOURCE_WINDOW_MIN_MS = 4_500")],"worker_source_window_limit"),
("output-length-max",[("LENGTH_MAX_MS = 9_000","LENGTH_MAX_MS = 10_000")],"long_run_prefix"),
("repeat-length",[("(previous is None or ms != previous.length_ms) and worker_admits_window","(True) and worker_admits_window"),("if candidate.length_ms == previous.length_ms:","if False:")],"long_run_prefix"),
("repeat-treatment",[("name != excluded and","True and"),("if candidate.treatment == previous.treatment:","if False:")],"long_run_prefix"),
("four-combo-repeat",[("if candidate.combo() in used_combos:","if False:")],"changing_only_offset"),
("origin-seconds",[("return int(round(self.length_ms * self.speed))","return int(round(self.length_ms / self.speed))")],"origin_span_is_actual_output"),
("reverse-prefix-cap",[("gate_passes and cap_allows and not reverse_day_used","gate_passes and True and not reverse_day_used")],"reverse_actually_draws"),
("reverse-day-cap",[("gate_passes and cap_allows and not reverse_day_used","gate_passes and cap_allows and True"), ('if et_day(other.get("renderedAt") or "") == render_day:', 'if False:')],"reverse_actually_draws"),
("reverse-actual-draw-day",[('or (row.get("state") == RENDERED\n                and et_day(row["renderedAt"]) == today)', 'or False')],"draw_refuses_reverse_on_actual_day"),
("reverse-preflight-day",[('_assert_reverse_render_day(rows, row, moment, page_id)\n                return', 'return')],"reverse_preflight_refuses_day"),
("reverse-render-day",[('if et_day(other.get("renderedAt") or "") == render_day:', 'if False:')],"delayed_reverse"),
("released-replay",[('if any(row.get("jobId") == job_id and row.get("state") == RELEASED for row in rows):', 'if False:')],"released_operation_never"),
("durable-capacity",[('if len(rows) > SCRAMBLER_HISTORY_HORIZON:', 'if False:')],"capacity_never_prunes"),
("bounded-read",[('if len(raw) > SCRAMBLER_HISTORY_MAX_BYTES:', 'if False:')],"history_read_is_bounded"),
("render-replay-clock",[('# render day, report clock or any copied posting evidence.\n                    return', '# render day, report clock or any copied posting evidence.\n                    row["renderedAt"] = moment\n                    _save_history_at(path, history)\n                    return')],"render_replay_never_moves"),
("existing-job-bootstrap",[('if known_jobs and not path.exists():', 'if False:')],"existing_jobs_prevent_unknown_bootstrap"),
("render-consecutive-length",[('if row["lengthMs"] == last["lengthMs"]:', 'if False:')],"release_cannot_create_actual_render_consecutive_repeat and length"),
("render-consecutive-treatment",[('if row["treatment"] == last["treatment"]:', 'if False:')],"release_cannot_create_actual_render_consecutive_repeat and treatment"),
("render-commit-order",[('other.get("renderOrder", 0), _iso_epoch(other["renderedAt"])', 'rows.index(other), _iso_epoch(other["renderedAt"])')],"actual_render_order_is_commit_order"),
("raw-posting-page",[('return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256', 'return (isinstance(value, str) and value.startswith("@") and bool(value.strip()) and len(value) <= 256')],"posting_observation_preserves_valid_raw"),
("render-preflight",[('_assert_render_sequence(rows, row)\n                _assert_reverse_render_day(rows, row, moment, page_id)', '_assert_reverse_render_day(rows, row, moment, page_id)')],"preflight_refuses_repeated_actual"),
("canonical-pair-geometry",[('if "originKind" in row and (', 'if False and (')],"private_canonical_identity"),
("canonical-pair-load",[('if "originKind" in row or "originId" in row:', 'if False:')],"private_canonical_identity"),
("canonical-pair-immutable",[('"originEndMs", "originKind", "originId")', '"originEndMs")')],"private_canonical_identity"),
("immutable-binding",[('if row.get("masterSha256") is not None and any(row.get(key) != item.get(key) for key in fields):', 'if False:')],"planned_binding_is_immutable"),
]
results=[]
def run(name,body,selector=None):
 p=out/(name+".py");p.write_text(body);compile(body,str(p),"exec")
 cmd=[sys.executable,str(out/"bootstrap.py"),str(root),str(p)]
 if selector:cmd.append(selector)
 r=subprocess.run(cmd,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
 (out/(name+".log")).write_text(r.stdout)
 return r
base=run("base",source)
results.append({"case":"base","exit":base.returncode,"passed":base.returncode==0})
if base.returncode:print(base.stdout);raise SystemExit("base failed; no mutant claims")
for name,changes,selector in cases:
 mutant=source
 for old,new in changes:
  assert old in mutant,(name,"needle missing",old)
  mutant=mutant.replace(old,new)
 (out/(name+".diff")).write_text("".join(difflib.unified_diff(source.splitlines(True),mutant.splitlines(True),fromfile="services/scrambler.py",tofile=name+".py")))
 r=run(name,mutant,selector)
 assertion=bool(re.search(r"^E\s+(?:assert\b|AssertionError|Failed: DID NOT RAISE)",r.stdout,re.M)) or "DID NOT RAISE" in r.stdout
 if name == "raw-posting-page":
  assertion = assertion or ("scrambler_posting_page_invalid" in r.stdout and "in mark_posting_observed" in r.stdout and "test_posting_observation_preserves_valid_raw" in r.stdout)
 if name == "replay-config":
  assertion = assertion or ("ValueError: scrambler_fast_share_invalid" in r.stdout and "in reserve_job_cuts" in r.stdout and "test_invalid_new_config_keeps_existing_replay" in r.stdout)
 red=r.returncode==1 and assertion and "ERROR collecting" not in r.stdout and "SyntaxError" not in r.stdout and "ImportError" not in r.stdout
 row={"case":name,"exit":r.returncode,"behaviorally_red":red}
 results.append(row);print(json.dumps(row),flush=True)
(out/"results.json").write_text(json.dumps(results,indent=2)+"\n")
raise SystemExit(0 if all(r.get("passed",r.get("behaviorally_red",False)) for r in results) else 1)
