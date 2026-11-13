"""Replay every build-set invoice through the running artifact and compare to an independent
SQL-derived expectation. Also enforces the wire contract on every single reply."""
import csv, json, time, urllib.request, collections

URL = "http://localhost:18000/respond"
VALID = {"PRICE_DELTA","QTY_MISMATCH","TOTAL_MATH","OFF_CONTRACT_ITEM","OFF_CONTRACT_FEE","DUPLICATE"}
SENTENCE = ("Review invoice {inv} (supplier {sup}, PO {po}) against its PO line and the supplier "
            "contract; decide whether to flag it for human review and give a short reason.")

with open("scratch/expected.csv") as fh:
    expected = {r["invoice_number"]: r["expected"] for r in csv.DictReader(fh)}

contract_violations, mismatches, latencies = [], [], []
got_counts = collections.Counter()

for inv, exp in expected.items():
    body = json.dumps({"messages": [{"role": "user",
                                     "content": SENTENCE.format(inv=inv, sup="Supplier_X", po="PO-000000")}]}).encode()
    req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        envelope = json.loads(resp.read())
    latencies.append(time.perf_counter() - t0)

    content = envelope["choices"][0]["message"]["content"]
    # wire contract: bare JSON object, exactly three keys, right types, closed fault set
    problems = []
    if not isinstance(content, str) or content.strip()[:1] != "{" or content.strip()[-1:] != "}":
        problems.append("content is not a bare JSON object")
    try:
        obj = json.loads(content)
    except Exception as exc:
        problems.append(f"unparseable: {exc}")
        obj = {}
    if set(obj) != {"flagged", "fault_type", "reason"}:
        problems.append(f"keys={sorted(obj)}")
    if not isinstance(obj.get("flagged"), bool):
        problems.append("flagged not bool")
    ft = obj.get("fault_type")
    if ft is not None and ft not in VALID:
        problems.append(f"fault_type={ft!r} outside closed set")
    if obj.get("flagged") is False and ft is not None:
        problems.append("fault_type set while flagged false")
    if not isinstance(obj.get("reason"), str) or not obj.get("reason", "").strip():
        problems.append("reason missing/empty")
    if problems:
        contract_violations.append((inv, problems, content[:160]))

    got = ft if obj.get("flagged") else "CLEAN"
    got_counts[got] += 1
    if got != exp:
        mismatches.append((inv, exp, got, obj.get("reason", "")[:120]))

n = len(expected)
latencies.sort()
print(f"invoices replayed      : {n}")
print(f"wire-contract failures : {len(contract_violations)}")
print(f"verdict mismatches     : {len(mismatches)}")
print(f"latency  p50 / p95 / max: {latencies[n//2]*1000:.0f} / {latencies[int(n*0.95)]*1000:.0f} / "
      f"{latencies[-1]*1000:.0f} ms")
print("distribution           :", dict(got_counts))
for inv, problems, snippet in contract_violations[:5]:
    print(f"  CONTRACT {inv}: {problems} :: {snippet}")
for inv, exp, got, reason in mismatches[:10]:
    print(f"  MISMATCH {inv}: expected {exp}, got {got} :: {reason}")
