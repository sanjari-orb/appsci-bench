#!/usr/bin/env python
"""Manual chat with the appsci agent user sim over its real HTTP service.

A human-in-the-loop check (not an automated test) that exercises the full outside-world path:

    spin up the FastAPI appsci-agent appsci-agent-user-sim service (uvicorn) bound to a task + persona
      -> poll /health  -> you type questions in a REPL
      -> the agent-side HTTP client POSTs /ask  -> the stakeholder's reply prints.

You play the applied-science agent interviewing the stakeholder. Makes live LLM calls, so it needs
real model credentials (set via the APPSCI_USERSIM_* env vars). Ctrl-D or 'quit' to exit.

Usage:
    .venv/bin/python scripts/chat_appsci_user_sim.py                          # order_to_cash, L1 (defaults)
    .venv/bin/python scripts/chat_appsci_user_sim.py --persona l3
    .venv/bin/python scripts/chat_appsci_user_sim.py --task order_to_cash --persona l2 \
        --model anthropic/claude-opus-4-8

Quality check for the adversarial overlay (PR: adversarial appsci agent user sim). ``--adversarial``
injects ``adversarial_pressures`` onto the chosen persona at runtime (a temp persona copy; the
on-disk file is untouched), so you can chat with the SAME persona twice -- once plain, once
adversarial -- and judge whether the overlay makes a humanly discernible difference:
    .venv/bin/python scripts/chat_appsci_user_sim.py --persona l3                       # plain
    .venv/bin/python scripts/chat_appsci_user_sim.py --persona l3 --adversarial         # all pressures
    .venv/bin/python scripts/chat_appsci_user_sim.py --persona l3 --adversarial pushback,bad_request

Model config comes from APPSCI_USERSIM_MODEL (or the shared MODEL_NAME default) plus
MODEL_API_KEY/MODEL_BASE_URL, plus the
provider-standard API key picked by the model prefix (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...);
--model just overrides APPSCI_USERSIM_MODEL for convenience. See docs/user_simulator_api.md.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import threading
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# A fixed token for this local single-process run (the agent client must echo it). Not a secret:
# the service only listens on localhost for the lifetime of this script.
_LOCAL_TOKEN = "local-manual-token"  # noqa: S105
_HEALTH_TIMEOUT_S = 15.0

# The adversarial pressure profiles (must match the user_persona schema's `adversarial_pressures`
# enum). `--adversarial` with no value applies all of them.
_ADVERSARIAL_PRESSURES = ("pushback", "time_pressure", "scope_expansion", "scope_contraction",
                          "bad_request", "confidence_inflation")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chat with the appsci-agent-user-sim service over HTTP.")
    p.add_argument("--task", default="order_to_cash", help="task id under tasks/ (default: order_to_cash)")
    p.add_argument("--persona", default="l1",
                   help="persona file stem under tasks/<task>/user_personas/ (default: l1)")
    p.add_argument("--model", default=None,
                   help="override APPSCI_USERSIM_MODEL (a LiteLLM model id)")
    p.add_argument("--adversarial", nargs="?", const="ALL", default=None, metavar="PRESSURES",
                   help="inject adversarial_pressures onto the chosen persona for this run "
                        "(quality check for the adversarial overlay). Bare flag = all pressures; "
                        f"or pass a comma-separated subset of {','.join(_ADVERSARIAL_PRESSURES)}.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    return p.parse_args()


def _resolve_paths(task: str, persona: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve and existence-check the spec.yaml + persona YAML for the chosen task/persona."""
    spec = REPO_ROOT / "tasks" / task / "spec.yaml"
    persona_path = REPO_ROOT / "tasks" / task / "user_personas" / f"{persona}.yaml"
    if not spec.exists():
        sys.exit(f"no spec at {spec}")
    if not persona_path.exists():
        sys.exit(f"no persona at {persona_path}")
    return spec, persona_path


def _apply_adversarial(persona_path: pathlib.Path, pressures_arg: str) -> tuple[pathlib.Path, list[str]]:
    """Return a temp persona path = the chosen persona + injected `adversarial_pressures`.

    Lets the same on-disk persona be chatted with both plain and adversarial without editing it:
    we load the YAML, set `adversarial_pressures`, and write a temp copy the service points at. The
    temp file is cleaned up at process exit. Returns (temp_path, pressures_applied).
    """
    import atexit
    import tempfile

    import yaml

    if pressures_arg == "ALL":
        pressures = list(_ADVERSARIAL_PRESSURES)
    else:
        pressures = [p.strip() for p in pressures_arg.split(",") if p.strip()]
        unknown = [p for p in pressures if p not in _ADVERSARIAL_PRESSURES]
        if unknown:
            sys.exit(f"unknown adversarial pressure(s): {unknown}; "
                     f"valid: {', '.join(_ADVERSARIAL_PRESSURES)}")

    persona = yaml.safe_load(persona_path.read_text())
    persona["adversarial_pressures"] = pressures

    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- kept open path; removed at exit
        mode="w", suffix=f"_{persona_path.stem}_adv.yaml", delete=False)
    tmp.write(yaml.safe_dump(persona, sort_keys=False))
    tmp.close()
    atexit.register(lambda: pathlib.Path(tmp.name).unlink(missing_ok=True))
    return pathlib.Path(tmp.name), pressures


def _wait_for_health(client, timeout_s: float) -> None:
    """Poll /health until ready or exit. (The agent would do the same before its first /ask.)"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.health():
                return
        except Exception:  # noqa: BLE001 -- server still coming up; keep polling
            pass
        time.sleep(0.2)
    sys.exit(f"service did not become healthy within {timeout_s:g}s")


def _repl(client) -> None:
    """Read questions from the terminal, send each to /ask, print the reply."""
    while True:
        try:
            question = input("you > ").strip()
        except EOFError:
            print()
            return
        if question.lower() in {"quit", "exit"}:
            return
        if not question:
            continue
        try:
            reply = client.ask(question)
        except Exception as exc:  # noqa: BLE001 -- show HTTP/model errors but keep the REPL alive
            print(f"  [error] {exc}")
            continue
        print(f"sim > {reply}\n")


def main() -> int:
    args = _parse_args()
    spec, persona_path = _resolve_paths(args.task, args.persona)

    pressures: list[str] = []
    if args.adversarial is not None:
        persona_path, pressures = _apply_adversarial(persona_path, args.adversarial)

    # Load model config (APPSCI_USERSIM_MODEL or MODEL_NAME, + MODEL_BASE_URL / a provider key)
    # from a local .env if present. See .env.sample. Real env vars already set take precedence.
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)

    # The service reads these at startup (build_session), so set them before create_app().
    os.environ["APPSCI_USERSIM_TOKEN"] = _LOCAL_TOKEN
    os.environ["APPSCI_USERSIM_SPEC_PATH"] = str(spec)
    os.environ["APPSCI_USERSIM_PERSONA_PATH"] = str(persona_path)
    if args.model:
        os.environ["APPSCI_USERSIM_MODEL"] = args.model

    import uvicorn
    from appsci_agent.clients import AppsciAgentUserSimClient
    from appsci_bench.appsci_agent_user_sim.service.app import create_app

    try:
        app = create_app()  # builds the Session (renders persona, constructs AppsciAgentLLMUserSim) from env
    except Exception as exc:  # noqa: BLE001 -- surface startup/config errors plainly
        sys.exit(f"failed to build the appsci-agent-user-sim service: {exc}")

    # Print the rendered system prompt up front so you can eyeball it for rendering failures.
    adv_label = f"  adversarial={','.join(pressures)}" if pressures else ""
    print("\n" + "=" * 80)
    print(f"SYSTEM PROMPT  (task={args.task}  persona={args.persona}{adv_label})")
    print("=" * 80)
    print(app.state.session.system_prompt)
    print("=" * 80)

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    base_url = f"http://{args.host}:{args.port}"
    os.environ["APPSCI_USERSIM_URL"] = base_url  # the agent client reads APPSCI_USERSIM_URL + APPSCI_USERSIM_TOKEN
    client = AppsciAgentUserSimClient()
    _wait_for_health(client, _HEALTH_TIMEOUT_S)

    print(f"\n  appsci-agent-user-sim ready at {base_url}")
    print(f"  task={args.task}  persona={args.persona}{adv_label}  "
          f"model={os.environ.get('APPSCI_USERSIM_MODEL') or os.environ.get('MODEL_NAME', 'default')}")
    print("  You are the applied scientist. Type a question; Ctrl-D or 'quit' to exit.\n")
    _repl(client)
    print("bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
