"""Verdicts must be identical under parallel load, and must survive a database restart."""
import csv, json, subprocess, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

with open("scratch/expected.csv") as fh:
    rows = [(r["invoice_number"], r["expected"]) for r in csv.DictReader(fh)]

def ask(pair):
    inv, exp = pair
    body = json.dumps({"messages": [{"role": "user", "content":
        f"Review invoice {inv} (supplier Supplier_X, PO PO-000000) against its PO line and the "
        f"supplier contract; decide whether to flag it and give a short reason."}]}).encode()
    req = urllib.request.Request("http://localhost:18000/respond", data=body,
                                 headers={"content-type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        obj = json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
    got = obj["fault_type"] if obj["flagged"] else "CLEAN"
    return (got == exp), time.perf_counter() - t0, obj["reason"]

for workers in (8, 32, 64):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(ask, rows))
    wall = time.perf_counter() - t0
    lat = sorted(d for _, d, _ in res)
    bad = [r for ok, _, r in res if not ok]
    print(f"{workers:3d}-way: {len(res)-len(bad)}/{len(res)} correct, {len(res)/wall:.0f} req/s, "
          f"p95 {lat[int(len(lat)*0.95)]*1000:.0f} ms, max {lat[-1]*1000:.0f} ms"
          + (f"  FIRST BAD: {bad[0][:90]}" if bad else ""))

print("\n-- database restart mid-flight (connections all go stale at once)")
subprocess.run(["docker", "restart", "o2c-db-1"], capture_output=True)
for _ in range(30):
    if subprocess.run(["docker", "exec", "o2c-db-1", "pg_isready", "-U", "sandbox", "-d", "oltp"],
                      capture_output=True).returncode == 0:
        break
    time.sleep(1)
with ThreadPoolExecutor(max_workers=16) as ex:
    res = list(ex.map(ask, rows[:120]))
bad = [r for ok, _, r in res if not ok]
print(f"after restart: {len(res)-len(bad)}/{len(res)} correct"
      + (f"  FIRST BAD: {bad[0][:90]}" if bad else "  (reconnected transparently)"))
