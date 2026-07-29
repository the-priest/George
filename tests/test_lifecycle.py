import os, sys, subprocess, time, tempfile
tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"; os.environ["XDG_DATA_HOME"] = tmp + "/data"
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + "/fakebin:" + os.environ["PATH"]
os.environ["GEORGE_OLLAMA"] = "http://127.0.0.1:21434"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george_core as C
fails = []
sup = C.OllamaSupervisor(C.load_config())
ok, msg = sup.ensure_running()
if not ok: fails.append("did not start: %s" % msg)
if not sup.owned or sup.state != "owned": fails.append("ownership wrong: %s" % sup.state)
pid = sup.proc.pid; grp = os.getpgid(pid)
sup2 = C.OllamaSupervisor(C.load_config()); sup2.ensure_running()
if sup2.owned: fails.append("second instance claimed ownership")
sup2.shutdown()
if not sup.client.alive(): fails.append("second instance killed a daemon it did not start")
sup.shutdown(); time.sleep(1.0)
if sup.client.alive(): fails.append("daemon alive after shutdown")
left = [x for x in subprocess.run(["pgrep","-g",str(grp)],capture_output=True,text=True).stdout.split() if x.strip()]
if left: fails.append("process group leftovers: %s" % left)
sup.shutdown(); sup.shutdown()
os.environ["PATH"] = "/nonexistent"
s3 = C.OllamaSupervisor(dict(C.DEFAULTS, ollama_url="http://127.0.0.1:21999"))
ok3, msg3 = s3.ensure_running()
if ok3 or "not installed" not in msg3: fails.append("no-binary path: %s" % msg3)
print("lifecycle failures:", len(fails))
for f in fails: print("  FAIL", f)
sys.exit(1 if fails else 0)
