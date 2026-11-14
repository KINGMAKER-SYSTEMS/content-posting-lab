"""Actual edited-source regression proofs; run remotely through suite-slot."""
import difflib, hashlib, json, os, re, subprocess, sys, tempfile
from pathlib import Path
root = Path(__file__).resolve().parents[1]
out = Path(os.environ.get("SCRAMBLER_MUTANT_OUT") or tempfile.mkdtemp(prefix="scrambler-integration-"))
out.mkdir(parents=True, exist_ok=True)
print("Evidence directory:",out,flush=True)
bootstrap = out/"bootstrap.py"
bootstrap.write_text("""import importlib.util,sys,pytest
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
name=sys.argv[2]
if name != 'baseline':
 parent=__import__(name.rpartition('.')[0],fromlist=['*'])
 spec=importlib.util.spec_from_file_location(name,sys.argv[3])
 m=importlib.util.module_from_spec(spec);m.__file__=str(root / (name.replace('.', '/')+'.py'));sys.modules[name]=m;setattr(parent,name.rpartition('.')[2],m);spec.loader.exec_module(m)
raise SystemExit(pytest.main([str(root/'tests/test_scrambler_setup_failures.py'),str(root/'tests/test_scrambler_canonical_reservations.py'),str(root/'tests/test_scrambler_executor_behavior.py'),str(root/'tests/test_source_most_different.py'),'-q','--tb=short',*sys.argv[4:]]))
""")
sources={name:(root/path).read_text() for name,path in [
 ("services.control_plane_sources","services/control_plane_sources.py"),
 ("routers.control_plane","routers/control_plane.py")]}
(out/"sources.sha256.json").write_text(json.dumps({k:hashlib.sha256(v.encode()).hexdigest() for k,v in sources.items()},indent=2))
def run(label,name,body,selector=None):
 path=out/(label+".py")
 path.write_text(body);compile(body,str(path),"exec")
 cmd=[sys.executable,str(bootstrap),str(root),name,str(path)]
 if selector:cmd+=["-k",selector]
 proc=subprocess.run(cmd,capture_output=True,text=True)
 log=proc.stdout+proc.stderr;(out/(label+".log")).write_text(log)
 red=proc.returncode==1 and bool(re.search(r"AssertionError|DID NOT RAISE|E\s+assert\b",log)) and not re.search(r"ImportError|ModuleNotFoundError|SyntaxError|ERROR collecting",log)
 return proc.returncode,red
rc,_=run("baseline","baseline","")
if rc:raise SystemExit("Baseline failed; no mutation claims")
cases=[
 ("pending-hold","services.control_plane_sources","held.append(window)","pass","pending_commitment"),
 ("canonical-second-bins","services.control_plane_sources",
  "and other_start // 1000 <= (end - 1) // 1000\n            and start // 1000 <= (other_end - 1) // 1000",
  "and other_start < end and start < other_end","adjacent_windows"),
 ("early-cleanup","routers.control_plane",
  'reason="execution_finished")','reason="execution_finished")' , "early_execution"),
 ("queue-retirement","routers.control_plane",
  'if not allowed:\n                        raise _ScramblerCutRefused("scrambler_execution_originality_refused:" + reason)',
  'if False:\n                        raise _ScramblerCutRefused("scrambler_execution_originality_refused:" + reason)',"queue_delay"),
 ("commitment-binding","routers.control_plane",
  'if (commitment is None or commitment["state"] != "reserved"',
  'if (False and (commitment is None or commitment["state"] != "reserved"',"queue_delay"),
 ("history-loss","routers.control_plane",
  'assert_existing_jobs(\n                        old_id','tuple(\n                        old_id',"missing_ledger"),
]
cases.extend([
 ("execute-kill-switch", "routers.control_plane",
  'if scramble and not scrambler_enabled(job["pageId"]):',
  'if False and scramble and not scrambler_enabled(job["pageId"]):',
  "on_enqueue_then_off_execute or on_enqueue_then_deny_execute"),
 ("receipt-speed", "routers.control_plane",
  'manifest["clipSpeed"] = cut_speed',
  'manifest["clipSpeed"] = render_speed',
  "on_execute_renders_drawn"),
 ("off-surface", "routers.control_plane",
  'cut_plan_seed = hashlib.sha256(',
  'cut_plan_seed = "mutant-" + hashlib.sha256(',
  "off_enqueue_serialization"),
 ("identity-hash", "routers.control_plane",
  '"sourceLibraryHash": source_recipe.source_library_hash,',
  '"sourceLibraryHash": ("sha256:scrambler-mutant" if scrambler_draws is not None else source_recipe.source_library_hash),',
  "on_job_preserves_every_library"),
 ("farthest-window", "services.control_plane_sources",
  '-float(distance) - rng.random() * 0.5',
  '+float(distance) - rng.random() * 0.5',
  "fingerprint_pick_maximises"),
 ("source-spacing", "services.control_plane_sources",
  'if require_far and near_recent(index, start_ms):',
  'if False and require_far and near_recent(index, start_ms):',
  "no_two_consecutive_cuts_within or recent_origin_start"),
])
cases.append(('cancel-checkpoint', 'routers.control_plane', 'reason="generation_cancelled")\n        await asyncio.to_thread(_update_job,\n            job_id,\n            status="failed",\n            error="generation_cancelled",\n', 'reason="generation_cancelled")\n        await asyncio.to_thread(_update_job,\n            job_id,\n            status="failed",\n            error="generation_cancelled",\n            **({"scramblerRenderedClips": manifests} if scramble else {}),\n', 'cancel_during_committed_thread'))
# These two guards span statements; construct compiling mutations explicitly.
cases[2]=(cases[2][0],cases[2][1],
 '''        await asyncio.to_thread(release_job_reservations, job["pageId"], job_id,
                                reason="execution_finished")''',
 '''        pass''',cases[2][4])
name,mod,old,new,sel=cases[4]
source=sources[mod]
begin=source.index('                    if (commitment is None')
end=source.index('                except (_ScramblerCutRefused',begin)
cases[4]=(name,mod,source[begin:end],"",sel)
results=[]
for label,name,old,new,selector in cases:
 source=sources[name]
 if old==new or old not in source:raise SystemExit("Mutation needle missing: "+label)
 mutated=source.replace(old,new,1)
 (out/(label+".diff")).write_text("".join(difflib.unified_diff(source.splitlines(True),mutated.splitlines(True))))
 rc,red=run(label,name,mutated,selector)
 results.append({"name":label,"exit":rc,"behaviorally_red":red})
 print(results[-1],flush=True)
(out/"results.json").write_text(json.dumps(results,indent=2))
raise SystemExit(0 if all(r["behaviorally_red"] for r in results) else 1)
