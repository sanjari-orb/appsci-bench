"""Native CLI launch adapters; deliberately no benchmark HTTP client or SDK.

Stdlib-only: this file is also the container entry point. Control handling and
validation are shared with the host; missing model/auth fails before task services.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

NATIVE_ENV = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
    "APPSCI_CODEX_RESPONSES_BASE_URL",
)

# These are reference-agent controls, not a common budget contract for native CLIs.
# Ignore them for BOTH native harnesses, even where a CLI has a similarly named flag.
IGNORED_CONTROLS = ("temperature", "max_tokens", "max_turns", "max_usd")


def build_controls(budgets: dict, *, model: str | None, max_turns: int | None,
                   temperature: float | None) -> dict:
    build = budgets.get("build") or {}
    return {
        "model": model, "temperature": temperature,
        "max_turns": max_turns if max_turns is not None else build.get("turns"),
        "max_tokens": build.get("tokens"), "max_usd": build.get("usd"),
    }


def effective_controls(controls: dict) -> tuple[dict, dict]:
    """Return native controls and ignored requests separately, without mutating input."""
    ignored = {name: controls[name] for name in IGNORED_CONTROLS
               if controls.get(name) is not None}
    return {**controls, **dict.fromkeys(IGNORED_CONTROLS)}, ignored


def required(env: dict, name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"native harness requires {name}")
    return value


def gateway_url(env: dict, name: str) -> str:
    value = required(env, name).rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an HTTP(S) base URL without credentials/query/fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{name} must use HTTPS except for a local gateway")
    return value


def custom_headers(raw: str) -> dict[str, str]:
    headers = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if (not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not value.strip() or name.lower() in {"authorization", "x-api-key", "host"}):
            raise ValueError("ANTHROPIC_CUSTOM_HEADERS must contain non-auth Name: Value lines")
        if name.lower() in {h.lower() for h in headers}:
            raise ValueError("ANTHROPIC_CUSTOM_HEADERS contains duplicate header names")
        headers[name] = value.strip()
    return headers


def validate_controls(adapter: str, controls: dict) -> None:
    """Credential-free validation, also used when accepting jobs on the sweep server."""
    if adapter not in {"codex", "claude-code"}:
        raise ValueError("native harness adapter must be codex or claude-code")
    model = controls.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("native harness requires --agent-model with the provider's native model ID")


def make_launch(adapter: str, auth: str, controls: dict, environ: dict) -> tuple[list[str], dict]:
    """Return argv and a child-only environment. Never put credentials in argv."""
    if auth not in {"api", "gateway"}:
        raise ValueError("native harness auth must be api or gateway")
    validate_controls(adapter, controls)
    controls, _ = effective_controls(controls)
    model = controls["model"]
    env = dict(environ)
    if adapter == "codex":
        if auth == "gateway":
            # Explicit opt-in: the eval simulator's OPENAI_BASE_URL proves only chat support.
            base_url = gateway_url(env, "APPSCI_CODEX_RESPONSES_BASE_URL")
            key = required(env, "ANTHROPIC_AUTH_TOKEN")
            headers = custom_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
        else:
            key = required(env, "OPENAI_API_KEY")
            if (env.get("OPENAI_BASE_URL", "").rstrip("/") not in {"", "https://api.openai.com/v1"}
                    or key == env.get("ANTHROPIC_AUTH_TOKEN")):
                raise ValueError("Codex api auth needs a first-party OPENAI_API_KEY and no gateway "
                                 "OPENAI_BASE_URL; use the gateway config for gateway credentials")
            base_url, headers = "https://api.openai.com/v1", {}
        env["APPSCI_NATIVE_API_KEY"] = key
        argv = ["codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                "--model", model]
        config = {
            "model_provider": "appsci",
            "model_providers.appsci.name": "AppSci benchmark",
            "model_providers.appsci.base_url": base_url,
            "model_providers.appsci.env_key": "APPSCI_NATIVE_API_KEY",
            "model_providers.appsci.wire_api": "responses",
            "model_providers.appsci.requires_openai_auth": False,
            # The agent's shell needs the benchmark endpoint tokens to use the HTTP contract.
            "shell_environment_policy.inherit": "all",
            "shell_environment_policy.ignore_default_excludes": True,
        }
        for index, (name, value) in enumerate(headers.items()):
            variable = f"APPSCI_NATIVE_HEADER_{index}"
            env[variable] = value
            config[f'model_providers.appsci.env_http_headers.{json.dumps(name)}'] = variable
        for name, value in config.items():
            argv += ["-c", f"{name}={json.dumps(value)}"]
        argv += ["-"]  # Complete benchmark prompt arrives on stdin, not a shell-expanded argument.
    else:
        # Provider routing is explicit and process-local; do not change the benchmark's roles.
        for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                     "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"):
            env.pop(name, None)
        if auth == "gateway":
            env["ANTHROPIC_BASE_URL"] = gateway_url(env, "ANTHROPIC_BASE_URL")
            required(env, "ANTHROPIC_AUTH_TOKEN")
            custom_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
            env.pop("ANTHROPIC_API_KEY", None)
        else:
            key = required(env, "ANTHROPIC_API_KEY")
            if key == env.get("ANTHROPIC_AUTH_TOKEN"):
                raise ValueError("ANTHROPIC_API_KEY is a gateway token, not a first-party key")
            for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"):
                env.pop(name, None)
        env["DISABLE_AUTOUPDATER"] = "1"
        argv = ["claude", "--print", "--verbose", "--output-format", "stream-json",
                "--dangerously-skip-permissions", "--model", model, "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--no-session-persistence"]
    return argv, env


def main() -> int:
    try:
        # Bypass modes are ONLY for the externally isolated benchmark container, never the host.
        if not Path("/.dockerenv").exists() or os.getuid() == 0:
            raise ValueError("native adapters must run as a non-root user inside the benchmark Docker image")
        context = json.loads(Path(required(os.environ, "APPSCI_HARNESS_CONTEXT")).read_text())
        if context.get("version") != 1:
            raise ValueError("unsupported harness input version")
        # The second source covers older bundles and standalone adapter callers.
        _, ignored = effective_controls(context["build_controls"])
        ignored = {**context.get("ignored_build_controls", {}), **ignored}
        if ignored:
            print("Native harness ignores reference-agent controls: "
                  + ", ".join(sorted(ignored)), file=sys.stderr, flush=True)
        argv, env = make_launch(required(os.environ, "APPSCI_HARNESS_ADAPTER"),
                                required(os.environ, "APPSCI_HARNESS_AUTH"),
                                context["build_controls"], dict(os.environ))
        prompt = Path(required(os.environ, "APPSCI_HARNESS_PROMPT"))
        if not prompt.read_text().strip():
            raise ValueError("empty harness prompt")
        os.chdir(context["paths"]["workspace"])
        with prompt.open("rb") as stream:
            os.dup2(stream.fileno(), 0)
        # Replace PID 1: the native exit status, logs and signals belong to the existing lifecycle.
        os.execvpe(argv[0], argv, env)
    except (ValueError, OSError, KeyError) as exc:
        print(f"Native harness startup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
