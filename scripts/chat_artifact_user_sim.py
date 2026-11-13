#!/usr/bin/env python
"""Manual chat with the artifact agent user sim, in-process (no HTTP service).

A human-in-the-loop check (not an automated test): the real sim drives every turn toward its
seeded user_task/user_persona, deciding for itself when to stop -- you play the ARTIFACT, typing
its reply at each `artifact>` prompt. This is the artifact-agent-user-sim's sibling to
`chat_appsci_user_sim.py`, but the two share almost no code: that script spins up an HTTP
microservice and puts the human on the QUESTIONER's side; this one drives an in-process session
object with no HTTP at all and puts the human on the *other* side of the conversation (playing the
artifact, not the agent).

No artifact is served anywhere in the repo yet (Part 3's biggest open risk), so this is the only
way to chat with the real sim today. A `--artifact-url` flag is reserved for when Part 1's Http
artifact-client transport is real, letting a genuinely served artifact answer itself instead of a
human typing replies -- not wired yet.

Files, both directions:
  - sim -> you: when the sim attaches one of its own files via `attach_files`, this script
    materializes it (reads `file_path` into bytes if it wasn't already) and writes a real copy to
    a fresh `scripts/_out/chat_artifact_user_sim_<run>/incoming/` folder, printing the path -- so
    you can actually open and look at what a real artifact would have received, not just its name.
  - you -> sim: after typing your reply, you're prompted for zero or more local file paths
    (comma-separated, blank = none) to attach to your `ArtifactAgentOutput`. `file_type` is
    inferred from each path's extension; an unsupported/missing extension is skipped with a
    warning rather than silently dropped or guessed wrong (`ALLOWED_FILE_TYPES` in datatypes.py).

Makes live LLM calls, so it needs real model credentials. This sim runs tau-bench's user-simulator
model (overridable with `ARTIFACT_USERSIM_MODEL` or `--model`), so by default it needs OpenAI
credentials (`OPENAI_API_KEY`) -- NOT the `MODEL_*` vars the build-time sim and eval judges use, which
are never inherited. `.env` is loaded automatically if present (see .env.sample).

Ctrl-D or 'quit' (typed as the artifact's reply) to exit early. Otherwise the conversation ends
when the sim itself decides `user_stop_criteria` is met, or after --max-turns.

Usage:
    # seed from a real dataset sample
    .venv/bin/python scripts/chat_artifact_user_sim.py --task airline_rebooking --data-point tau2_airline_5

    # seed ad hoc
    .venv/bin/python scripts/chat_artifact_user_sim.py \\
        --user-task "You want a refund for your delayed flight." \\
        --persona "You are Alex Rivera, a Gold member, mildly impatient." \\
        --stop-criteria "the refund has been processed"

    .venv/bin/python scripts/chat_artifact_user_sim.py --task airline_rebooking --data-point tau2_airline_5 \\
        --model anthropic/claude-opus-4-8 --max-turns 6
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_OUT_ROOT = REPO_ROOT / "scripts" / "_out"  # gitignored; each run gets a fresh subfolder
_DEFAULT_MAX_TURNS = 10


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Chat with the artifact agent user sim in-process (you play the artifact).")
    p.add_argument("--task", default=None, help="task id under tasks/ to seed from (with --data-point)")
    p.add_argument("--data-point", default=None, help="data_point_id within --task's dataset")
    p.add_argument("--user-task", default=None, help="ad hoc user_task (alternative to --task/--data-point)")
    p.add_argument("--persona", default=None, help="ad hoc user_persona")
    p.add_argument("--stop-criteria", default=None,
                   help="ad hoc user_stop_criteria (default: the dataset's documented default)")
    p.add_argument("--model", default=None,
                   help="override ARTIFACT_USERSIM_MODEL (a LiteLLM model id)")
    p.add_argument("--max-turns", type=int, default=_DEFAULT_MAX_TURNS,
                   help=f"cap on real turns before giving up (default: {_DEFAULT_MAX_TURNS})")
    p.add_argument("--artifact-url", default=None,
                   help="RESERVED, not wired yet -- would let a genuinely served artifact answer "
                        "itself instead of a human typing replies (needs Part 1's Http transport).")
    return p.parse_args()


def _resolve_seed(args: argparse.Namespace):
    from appsci_bench.eval.datatypes import MultiTurnInput

    if args.task or args.data_point:
        if not (args.task and args.data_point):
            sys.exit("--task and --data-point must be given together")
        from appsci_bench.core.spec import load_task_spec
        from appsci_bench.eval.dataset import load_dataset
        spec = load_task_spec(args.task)
        dataset = load_dataset(spec)
        sample = next((s for s in dataset.data if s.data_point_id == args.data_point), None)
        if sample is None:
            sys.exit(f"no data point {args.data_point!r} in {args.task}'s dataset")
        if sample.multi_turn_input is None:
            sys.exit(f"{args.data_point!r} is a single_turn sample, not multi_turn")
        return sample.multi_turn_input

    if args.user_task or args.persona:
        if not (args.user_task and args.persona):
            sys.exit("--user-task and --persona must be given together")
        kwargs = {"user_task": args.user_task, "user_persona": args.persona}
        if args.stop_criteria:
            kwargs["user_stop_criteria"] = args.stop_criteria
        return MultiTurnInput(**kwargs)

    sys.exit("pass either --task + --data-point, or --user-task + --persona")


def _ask_with_retry(sim, session_id: str, conversation: list):
    """Call `sim.ask()`, offering a retry (same conversation state) on failure -- a single bad
    model turn (e.g. a parse error from a model that dropped a required JSON key) shouldn't cost
    the whole manual session.

    Returns `(gave_up, query)`: `gave_up` is True iff the human declined to retry after a failure
    (`query` is then meaningless, always None); otherwise `query` is the real result, which may
    itself legitimately be None -- that's the sim's own stop signal, not a failure.
    """
    while True:
        try:
            return False, sim.ask(session_id, conversation)
        except Exception as exc:  # noqa: BLE001 -- show model/parse errors but keep the REPL alive
            print(f"  [error] {exc}")
            try:
                choice = input("  retry this turn? [Y/n] ").strip().lower()
            except EOFError:
                print()
                return True, None
            if choice == "n":
                return True, None


def _save_incoming_files(query, incoming_dir: pathlib.Path) -> None:
    """Materialize + write each of the sim's attached files to `incoming_dir`, printing the path --
    the real bytes a served artifact would receive, not just a name."""
    from appsci_bench.eval.datatypes import materialize_files

    if not query.files:
        return
    materialize_files(query)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    for f in query.files:
        dest = incoming_dir / f.file_name
        dest.write_bytes(f.file_data)
        print(f"      attach_files: {f.file_name} ({f.file_type}) -> saved to {dest}")


def _read_outgoing_files() -> list:
    """Prompt for zero or more local file paths to attach to the artifact's reply."""
    from appsci_bench.eval.datatypes import ALLOWED_FILE_TYPES, File

    try:
        raw = input("  attach file(s)? (comma-separated local paths, blank for none) > ").strip()
    except EOFError:
        print()
        return []
    if not raw:
        return []

    files = []
    for part in raw.split(","):
        path_str = part.strip()
        if not path_str:
            continue
        path = pathlib.Path(path_str).expanduser()
        if not path.is_file():
            print(f"  [skip] no such file: {path}")
            continue
        file_type = path.suffix.lstrip(".").lower()
        if file_type not in ALLOWED_FILE_TYPES:
            print(f"  [skip] {path.name}: file_type {file_type!r} not in {ALLOWED_FILE_TYPES}")
            continue
        files.append(File(file_type=file_type, file_name=path.name, file_data=path.read_bytes()))
    return files


def _repl(sim, session_id: str, max_turns: int, incoming_dir: pathlib.Path) -> None:
    from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn

    conversation: list[ConversationTurn] = []
    gave_up, query = _ask_with_retry(sim, session_id, conversation)
    turns = 0
    while True:
        if gave_up:
            return
        if query is None:
            print("\n  [sim stopped -- its stop criteria was met]")
            return
        if turns >= max_turns:
            print(f"\n  [max_turns ({max_turns}) reached without the sim stopping]")
            return
        print(f"\nsim > {query.text}")
        _save_incoming_files(query, incoming_dir)
        try:
            reply = input("artifact > ").strip()
        except EOFError:
            print()
            return
        if reply.lower() in {"quit", "exit"}:
            return
        outgoing_files = _read_outgoing_files()
        conversation.append(
            ConversationTurn(query, ArtifactAgentOutput(text=reply, files=outgoing_files)))
        turns += 1
        gave_up, query = _ask_with_retry(sim, session_id, conversation)


def main() -> int:
    args = _parse_args()

    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
    if args.model:
        import os
        os.environ["ARTIFACT_USERSIM_MODEL"] = args.model

    mti = _resolve_seed(args)

    from appsci_bench.eval.artifact_agent_user_sim import (
        ArtifactAgentUserSim, resolve_user_sim_config,
    )
    # Resolve explicitly (rather than letting the sim do it internally) so the banner can print the
    # model actually in effect, including the tau-bench default when nothing is overridden.
    cfg = resolve_user_sim_config()
    sim = ArtifactAgentUserSim(config=cfg)
    session_id = sim.create_session(mti)

    _OUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="chat_artifact_user_sim_", dir=str(_OUT_ROOT)))
    incoming_dir = run_dir / "incoming"

    print("\n" + "=" * 80)
    print("SEED")
    print("=" * 80)
    print(f"user_task:          {mti.user_task}")
    print(f"user_persona:       {mti.user_persona}")
    print(f"user_stop_criteria: {mti.user_stop_criteria}")
    print(f"files:              {[f.file_name for f in mti.files] or '(none)'}")
    print(f"model:              {cfg.model}")
    print(f"incoming files dir: {incoming_dir}")
    print("=" * 80)
    print("\nYou are the ARTIFACT. The sim drives; type its replies, and optionally attach files")
    print("of your own when prompted. Ctrl-D or 'quit' to exit.\n")

    try:
        _repl(sim, session_id, args.max_turns, incoming_dir)
    finally:
        sim.delete_session(session_id)
    print("bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
