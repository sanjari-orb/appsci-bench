#!/usr/bin/env python
"""Interactive chat client for testing the agent by hand.

The conversation is held here, in the client, and the whole transcript is posted to the artifact's
/respond on every turn -- exactly what the real caller does. That is why /new simply forgets the
transcript: there is no server-side session to clear.

    python chat.py                               talk to the agent
    python chat.py --persona cancel_within_24h_0 start from a mined test persona
    python chat.py --hash                        print the database fingerprint and exit
    python chat.py --reset                       restore the pristine database and exit

In-session commands: /hash  /reset  /new  /persona [id]  /personas  /quit

See RUNBOOK.md for the safe testing workflow. In short: read-only scenarios need no reset, and
anything that writes should be followed by /reset.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import httpx

ARTIFACT_URL = os.environ.get("ARTIFACT_URL", "http://localhost:8080")
ENV_URL = os.environ.get("ENV_ADMIN_URL", "http://localhost:8080")
ADMIN_TOKEN = os.environ.get("AIRLINE_ENV_ADMIN_TOKEN", "airline-admin-token-local")

CASES_PATH = pathlib.Path(__file__).resolve().parent / "tests" / "cases.json"

# The fingerprint of the untouched fixture. If /hash prints this, the database is pristine.
PRISTINE_HASH = "44b4b7da83f81d77ba17ed94a7e51f81995a8a23fa8df1df702c8df80dddd0fb"


def load_cases() -> dict:
    """The mined personas, keyed by id (empty if cases.json has not been generated)."""
    if not CASES_PATH.exists():
        return {}
    return {c["id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}


def state_hash() -> str:
    """The environment's current state fingerprint, via the admin route."""
    resp = httpx.get(f"{ENV_URL}/admin/state_hash",
                     headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["state_hash"]


def reset() -> str:
    """Restore the pristine fixture. Resets the WHOLE environment -- see RUNBOOK.md."""
    resp = httpx.post(f"{ENV_URL}/admin/reset",
                      headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("status", "reset")


def describe_hash() -> str:
    """The current hash, annotated with whether it matches the pristine fixture."""
    try:
        current = state_hash()
    except Exception as exc:  # noqa: BLE001 - a diagnostic helper must not crash the session
        return f"could not read state hash: {exc}"
    label = "PRISTINE" if current == PRISTINE_HASH else "MODIFIED (run /reset to restore)"
    return f"{current[:16]}...  {label}"


def show_persona(case: dict) -> None:
    """Print a persona's ids, facts and expected outcome so the tester knows what right looks like."""
    print(f"\n--- persona {case['id']} ({case['branch']}) ---")
    print(f"  user id:        {case['user_id']}")
    print(f"  reservation id: {case['reservation_id']}")
    print(f"  expected:       {case['expect']['outcome']}")
    print(f"  why:            {case['why']}")
    if case["expect"]["forbidden_tools"]:
        print(f"  must NOT call:  {', '.join(case['expect']['forbidden_tools'])}")
    print(f"  facts:          {json.dumps(case['facts'])}")
    print(f"\n  suggested opening line:\n    {case['user_turns'][0]}\n")


def send(messages: list[dict], url: str) -> str:
    """Post the transcript to the artifact and return the assistant's reply."""
    resp = httpx.post(f"{url}/respond", json={"messages": messages}, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=ARTIFACT_URL, help="artifact base URL")
    parser.add_argument("--persona", help="start from a mined persona id")
    parser.add_argument("--personas", action="store_true", help="list personas and exit")
    parser.add_argument("--hash", action="store_true", help="print the state hash and exit")
    parser.add_argument("--reset", action="store_true", help="reset the database and exit")
    args = parser.parse_args()

    url = args.url
    cases = load_cases()

    if args.personas:
        for case in cases.values():
            print(f"{case['id']:<42} {case['expect']['outcome']:<12} {case['branch']}")
        return
    if args.hash:
        print(describe_hash())
        return
    if args.reset:
        print(f"{reset()}: {describe_hash()}")
        return

    print(f"airline agent chat -- {url}")
    print(f"database: {describe_hash()}")
    print("commands: /hash  /reset  /new  /persona [id]  /personas  /quit\n")

    messages: list[dict] = []
    if args.persona:
        if args.persona not in cases:
            sys.exit(f"unknown persona {args.persona!r}; try --personas")
        show_persona(cases[args.persona])

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue

        if line in {"/quit", "/exit"}:
            return
        if line == "/hash":
            print(describe_hash())
            continue
        if line == "/reset":
            print(f"{reset()}: {describe_hash()}")
            continue
        if line == "/new":
            messages = []
            print("(conversation cleared)")
            continue
        if line == "/personas":
            for case in cases.values():
                print(f"{case['id']:<42} {case['expect']['outcome']:<12} {case['branch']}")
            continue
        if line.startswith("/persona"):
            parts = line.split(maxsplit=1)
            if len(parts) == 1:
                print("usage: /persona <id>   (see /personas)")
            elif parts[1] in cases:
                messages = []
                show_persona(cases[parts[1]])
            else:
                print(f"unknown persona {parts[1]!r}")
            continue

        messages.append({"role": "user", "content": line})
        try:
            reply = send(messages, url)
        except Exception as exc:  # noqa: BLE001 - keep the session alive on a transport error
            print(f"error: {exc}")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        print(f"\nagent> {reply}\n")


if __name__ == "__main__":
    main()
