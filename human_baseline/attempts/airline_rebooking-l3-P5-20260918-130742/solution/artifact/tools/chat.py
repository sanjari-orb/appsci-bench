#!/usr/bin/env python3
"""Talk to the agent from your terminal, or let a scripted persona talk to it for you.

Two modes:

  interactive -- you type, the agent answers. Default.
  persona     -- a scripted customer from tests/simulator.py drives the conversation, including the
                 ones who lie and then push back.

Either way the agent is the real thing: the real policy layer, the real write gating, and the real task
environment. Writes really happen, so `--dry-run` is offered and the script always prints the DB state
hash before and after, with a reset hint when it moved.

Usage:
    python tools/chat.py                          # interactive
    python tools/chat.py --persona lying_about_cancellation
    python tools/chat.py --persona all            # run every persona in turn
    python tools/chat.py --list                   # show the personas
    python tools/chat.py --dry-run                # block every write, for safe poking around
    python tools/chat.py --trace                  # show each tool call as it happens

Environment:
    AIRLINE_ENV_URL   defaults to http://localhost:18080 here, because the task environment's port is
                      not published to the host. Run `tools/dev_proxy.sh up` once to open that tunnel.
    ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) must be set, or there is no agent to talk to.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ssl_context() -> ssl.SSLContext | None:
    """A TLS context that can actually verify the API's certificate.

    A python.org framework build on macOS ships without wiring the system trust store into OpenSSL
    (its "Install Certificates.command" is what normally does that), so the default context raises
    CERTIFICATE_VERIFY_FAILED. `certifi` usually comes along with such an install, so prefer its CA
    bundle when the default context has no usable roots. Returns None to mean "the default is fine".
    """
    default = ssl.create_default_context()
    if default.cert_store_stats().get("x509_ca", 0) > 0:
        return None
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())

from agent.orchestrator import Orchestrator  # noqa: E402
from config import load_config  # noqa: E402
from env_client import EnvClient, ToolCallError, ToolRejection  # noqa: E402

# The task environment is not published to the host, so default to the dev proxy rather than to the
# container name (which only resolves from inside the docker network).
os.environ.setdefault("AIRLINE_ENV_URL", "http://localhost:18080")

ADMIN_TOKEN = os.environ.get("AIRLINE_ENV_ADMIN_TOKEN", "airline-admin-token-local")
ENV_CONTAINER = os.environ.get("AIRLINE_ENV_CONTAINER", "airline-workspace-1")

# Resolved once: None means the interpreter's default TLS trust store is usable.
_SSL_CONTEXT = _ssl_context()

BOLD, DIM, GREEN, YELLOW, RED, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"
)


# --- the model call -------------------------------------------------------------------------------

def build_llm(model: str, trace: bool):
    """Return a callable ``(system, messages, tools) -> {text, tool_calls}`` over the Messages API."""
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("CLAUDE_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or ""
    )
    extra: dict[str, str] = {}
    for part in (os.environ.get("ANTHROPIC_CUSTOM_HEADERS") or "").split(","):
        if ":" in part:
            name, value = part.split(":", 1)
            extra[name.strip()] = value.strip()

    def llm(system, messages, tools):
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": Orchestrator._to_anthropic_messages(messages),
            "tools": tools,
        }
        request = urllib.request.Request(
            f"{base}/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "authorization": f"Bearer {key}",
                "x-api-key": key,
                **extra,
            },
        )
        body = json.load(urllib.request.urlopen(request, timeout=90, context=_SSL_CONTEXT))
        text, calls = [], []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    {"id": block["id"], "name": block["name"],
                     "arguments": block.get("input") or {}}
                )
        if trace and calls:
            for c in calls:
                print(f"    {DIM}-> {c['name']}({json.dumps(c['arguments'])[:110]}){RESET}")
        return {"text": "".join(text), "tool_calls": calls}

    return llm, bool(key)


# --- a client that can refuse to write ------------------------------------------------------------

class DryRunClient(EnvClient):
    """An EnvClient that reads for real but refuses every write.

    The refusal is delivered as a `ToolRejection`, which is how the agent already handles a environment
    that says no -- so the conversation continues naturally instead of falling over.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked: list[dict] = []

    def call(self, name, arguments=None):
        from env_client import WRITE_TOOLS

        if name in WRITE_TOOLS:
            self.blocked.append({"tool": name, "arguments": arguments or {}})
            print(f"    {YELLOW}[dry-run] blocked {name}({json.dumps(arguments or {})[:110]}){RESET}")
            raise ToolRejection(name, "Dry-run mode: this change was not applied.")
        return super().call(name, arguments)


# --- environment helpers --------------------------------------------------------------------------

def state_hash() -> str | None:
    """The environment's DB fingerprint, via docker exec (the admin route is not host-reachable)."""
    import subprocess

    try:
        out = subprocess.run(
            ["docker", "exec", ENV_CONTAINER, "curl", "-s",
             "-H", f"Authorization: Bearer {ADMIN_TOKEN}",
             "http://localhost:8080/admin/state_hash"],
            capture_output=True, text=True, timeout=15,
        )
        return json.loads(out.stdout).get("state_hash")
    except Exception:
        return None


def preflight_llm(llm) -> bool:
    """Check the model is actually reachable before starting a conversation.

    Without this, a machine-level problem (most often a python.org build with no CA certificates) only
    shows up as a traceback on the first message, with the agent's safe fallback making it look as though
    the agent itself were broken.
    """
    try:
        llm("Reply with the single word OK.", [{"role": "user", "content": "ping"}], [])
        return True
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            print(f"{RED}Cannot verify the API's TLS certificate.{RESET}")
            print("This Python has no CA certificates installed -- it is a machine setup issue, not the")
            print("agent. Fix it once with the installer that shipped with Python:\n")
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            print(f"    {BOLD}/Applications/Python\\ {version}/Install\\ Certificates.command{RESET}\n")
            print(f"{DIM}or install certifi for this interpreter: "
                  f"{os.path.basename(sys.executable)} -m pip install certifi{RESET}")
        else:
            print(f"{RED}Cannot reach the model API:{RESET} {reason}")
        return False
    except Exception as exc:
        print(f"{RED}Model call failed:{RESET} {type(exc).__name__}: {exc}")
        return False


def preflight(client: EnvClient) -> bool:
    try:
        if client.health():
            return True
    except Exception:
        pass
    print(f"{RED}Cannot reach the task environment at {client.base_url}.{RESET}")
    print("Its port is not published to the host, so open the dev tunnel first:")
    print(f"    {BOLD}tools/dev_proxy.sh up{RESET}")
    return False


# --- the two modes --------------------------------------------------------------------------------

def run_interactive(orch: Orchestrator, client: EnvClient) -> None:
    print(f"{BOLD}You are the customer. Type your message; 'quit' ends the call.{RESET}")
    print(f"{DIM}Tip: the agent needs a user id. Try one from tests/db_snapshot.json,")
    print(f"e.g. rafael_moreau_9821 (gold) with reservation TRN009.{RESET}\n")

    conversation: list[dict[str, str]] = []
    while True:
        try:
            said = input(f"{CYAN}you   > {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not said:
            continue
        if said.lower() in {"quit", "exit", ":q"}:
            break

        conversation.append({"role": "user", "content": said})
        try:
            reply = orch.respond(conversation)
        except Exception as exc:  # never crash the session on one bad turn
            reply = f"<error {type(exc).__name__}: {exc}>"
        conversation.append({"role": "assistant", "content": reply})
        print(f"{GREEN}agent > {RESET}{reply}\n")

    report(client, orch)


def run_persona(name: str, orch: Orchestrator, client: EnvClient, max_turns: int) -> None:
    from tests import simulator

    persona = next((p for p in simulator.ALL_PERSONAS if p.name == name), None)
    if persona is None:
        print(f"{RED}No persona called {name!r}.{RESET} Use --list to see them.")
        return

    print(f"{BOLD}persona:{RESET} {persona.name}"
          f"{'  (lies about something)' if not persona.truthful else ''}")
    print(f"{BOLD}expected:{RESET} {persona.expectation}\n")

    sim = simulator.UserSimulator(persona)

    def agent_turn(messages):
        try:
            return orch.respond(messages)
        except Exception as exc:
            return f"<error {type(exc).__name__}: {exc}>"

    transcript = simulator.run_conversation(sim, agent_turn, max_turns=max_turns)
    for message in transcript:
        who = f"{CYAN}you   >{RESET}" if message["role"] == "user" else f"{GREEN}agent >{RESET}"
        print(f"{who} {message['content']}\n")

    report(client, orch)


def report(client: EnvClient, orch: Orchestrator) -> None:
    """Print what the conversation actually did to the database."""
    reads = [c for c in client.call_log if not c["is_write"]]
    writes = [c for c in client.call_log if c["is_write"] and not c["error"]]
    transfers = [c for c in client.call_log if c["tool"] == "transfer_to_human_agents"]
    blocked = getattr(client, "blocked", [])

    print(f"{BOLD}{'-' * 70}{RESET}")
    print(f"reads: {len(reads)}   writes: {len(writes)}   transfers: {len(transfers)}"
          f"{f'   blocked by dry-run: ' + str(len(blocked)) if blocked else ''}")
    for w in writes:
        print(f"  {YELLOW}WROTE{RESET} {w['tool']}({json.dumps(w['arguments'])[:120]})")
    if orch.dropped_extra_calls:
        print(f"  {DIM}dropped {orch.dropped_extra_calls} extra tool call(s) "
              f"(one call per turn is enforced){RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--persona", help="scripted customer to play, or 'all'")
    parser.add_argument("--list", action="store_true", help="list the personas and exit")
    parser.add_argument("--dry-run", action="store_true", help="block every write")
    parser.add_argument("--trace", action="store_true", help="print each tool call")
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL", "claude-sonnet-5"))
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args()

    if args.list:
        from tests import simulator

        print(f"{BOLD}personas{RESET} (* = tells a lie)\n")
        for p in simulator.ALL_PERSONAS:
            mark = "*" if not p.truthful else " "
            print(f" {mark} {p.name:34} {p.expectation}")
        return 0

    # The agent's own logs would drown the conversation; keep only real problems.
    logging.basicConfig(level=logging.ERROR)

    config = load_config()
    llm, has_key = build_llm(args.model, args.trace)
    if not has_key:
        print(f"{RED}No LLM credential found.{RESET} Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN).")
        print("Without it the agent can only return its safe fallback message.")
        return 1

    client_cls = DryRunClient if args.dry_run else EnvClient
    client = client_cls(config.env_url, config.env_token)
    if not preflight(client):
        return 1
    if not preflight_llm(llm):
        return 1

    before = state_hash()
    print(f"{DIM}env {config.env_url} | model {args.model}"
          f"{' | DRY RUN (no writes)' if args.dry_run else ''}{RESET}")
    if before:
        print(f"{DIM}db before: {before[:16]}...{RESET}\n")

    if args.persona:
        from tests import simulator

        names = (
            [p.name for p in simulator.ALL_PERSONAS]
            if args.persona == "all"
            else [args.persona]
        )
        for name in names:
            print(f"\n{BOLD}{'=' * 70}{RESET}")
            # A fresh client and orchestrator per persona: the verified-facts cache, the pending-action
            # table and the write-once guard are all per conversation by design.
            per_client = client_cls(config.env_url, config.env_token)
            run_persona(name, Orchestrator(config, per_client, llm=llm), per_client, args.max_turns)
    else:
        run_interactive(Orchestrator(config, client, llm=llm), client)

    after = state_hash()
    if before and after:
        moved = before != after
        colour = YELLOW if moved else GREEN
        print(f"\n{colour}db after : {after[:16]}... "
              f"({'CHANGED' if moved else 'unchanged'}){RESET}")
        if moved:
            print(f"{DIM}restore the pristine store with:  tools/dev_proxy.sh reset{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
