"""Integration tests for the Applied AI Scientist agent.

These tests verify the agent's components work together correctly.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from appsci_agent.core import AgentConfig
from appsci_agent.core.config import parse_custom_headers
from appsci_agent.tools.base import ToolContext, ToolResult
from appsci_agent.tools.talk_to_user import TalkToUserTool
from appsci_agent.tools.complete import CompleteTool
from appsci_agent.artifact import ArtifactBuilder, ArtifactValidationResult
from appsci_agent.prompts import PromptBuilder, build_system_prompt
from appsci_agent.clients import AppsciAgentUserSimClientError


class TestAgentConfig:
    """Tests for AgentConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = AgentConfig()
        assert config.llm_provider == "anthropic"
        assert config.model == "claude-opus-4-8"
        assert config.max_turns == 40
        assert config.max_tokens == 8192

    def test_config_from_dict(self):
        """Test creating config from a dictionary."""
        data = {
            "llm_provider": "openai",
            "model": "gpt-4",
            "max_turns": 20,
        }
        config = AgentConfig.from_dict(data)
        assert config.llm_provider == "openai"
        assert config.model == "gpt-4"
        assert config.max_turns == 20

    def test_config_from_env(self):
        """Test creating config from environment variables (auth only)."""
        with patch.dict("os.environ", {
            "APPSCI_AGENT_API_KEY": "test-key",
            "APPSCI_AGENT_BASE_URL": "https://proxy.example.com",
        }):
            config = AgentConfig.from_env()
            assert config.api_key == "test-key"
            assert config.base_url == "https://proxy.example.com"
            # Non-auth settings use defaults
            assert config.model == "claude-opus-4-8"
            assert config.max_turns == 40

    def test_config_from_env_parses_custom_headers(self):
        """The gateway's identity headers (gateway's X-Gateway-User) come in as one env var."""
        with patch.dict("os.environ", {
            "APPSCI_AGENT_CUSTOM_HEADERS": "X-Gateway-User: someone@example.com\nX-C: 1.0.33",
        }):
            config = AgentConfig.from_env()
        assert config.custom_headers == {
            "X-Gateway-User": "someone@example.com",
            "X-C": "1.0.33",
        }

    def test_config_from_env_without_custom_headers(self):
        # None (not {}) so the litellm parameter is omitted entirely for non-gateway users.
        with patch.dict("os.environ", {}, clear=True):
            assert AgentConfig.from_env().custom_headers is None
        # A malformed value degrades to "no extra headers" rather than breaking every call.
        with patch.dict("os.environ", {"APPSCI_AGENT_CUSTOM_HEADERS": "garbage"}, clear=True):
            assert AgentConfig.from_env().custom_headers is None

    def test_get_litellm_model(self):
        """Test litellm model string generation."""
        config = AgentConfig(llm_provider="anthropic", model="claude-3-opus")
        assert config.get_litellm_model() == "claude-3-opus"

    def test_mini_swe_inherits_the_agent_model(self):
        """The coding sub-agent follows the agent under test.

        It does the implementation work and is most of a run's spend, so a sub-agent left on a
        fixed model would make a model-comparison sweep report a difference far smaller than the
        models'. Covers the sweep's own path (the agent model arriving as an env override) as well
        as a directly constructed config.
        """
        assert AgentConfig(model="claude-haiku-4-5").mini_swe_model == "claude-haiku-4-5"
        assert AgentConfig.from_dict({"model": "claude-sonnet-4-6"}).mini_swe_model == (
            "claude-sonnet-4-6"
        )
        with patch.dict("os.environ", {"AGENT_MODEL": "claude-sonnet-4-6"}, clear=True):
            assert AgentConfig.from_env().mini_swe_model == "claude-sonnet-4-6"
        # Unset on both sides still lands on the agent's own default, not a second hardcoded id.
        assert AgentConfig().mini_swe_model == AgentConfig().model

    def test_mini_swe_model_can_still_be_named_explicitly(self):
        """Studying the sub-agent separately stays possible; it just has to be asked for."""
        config = AgentConfig(model="claude-haiku-4-5", mini_swe_model="claude-opus-4-8")
        assert config.mini_swe_model == "claude-opus-4-8"
        assert config.model == "claude-haiku-4-5"

        config = AgentConfig(llm_provider="custom", model="my-model")
        assert config.get_litellm_model() == "custom/my-model"


class TestParseCustomHeaders:
    """The gateway header block (gateway's ANTHROPIC_CUSTOM_HEADERS -> APPSCI_AGENT_CUSTOM_HEADERS)
    is parsed defensively: a malformed value must degrade to "no extra headers", never raise in the
    middle of an unattended run."""

    def test_multiple_lines(self):
        raw = "X-Gateway-User: someone@example.com\nX-Gateway-Client: 1.0.33-2606222233-94c1f6d"
        assert parse_custom_headers(raw) == {
            "X-Gateway-User": "someone@example.com",
            "X-Gateway-Client": "1.0.33-2606222233-94c1f6d",
        }

    def test_single_line(self):
        assert parse_custom_headers("X-Gateway-User: a@b.com") == {"X-Gateway-User": "a@b.com"}

    def test_splits_on_first_colon_only(self):
        # A value containing colons (a URL, a timestamp) must survive intact.
        assert parse_custom_headers("X-Origin: https://gw:8443/v1") == {
            "X-Origin": "https://gw:8443/v1",
        }

    def test_whitespace_is_stripped(self):
        assert parse_custom_headers("  X-A :  v  \n\n  X-B:v2  ") == {"X-A": "v", "X-B": "v2"}

    def test_line_without_colon_is_ignored(self):
        assert parse_custom_headers("nonsense\nX-A: v") == {"X-A": "v"}

    def test_malformed_only_yields_no_headers(self):
        assert parse_custom_headers("nonsense") == {}

    def test_blank_and_none(self):
        assert parse_custom_headers("") == {}
        assert parse_custom_headers("   \n  ") == {}
        assert parse_custom_headers(None) == {}

    def test_empty_value_is_kept_but_empty_name_is_not(self):
        assert parse_custom_headers("X-A:") == {"X-A": ""}
        assert parse_custom_headers(": v") == {}

    @pytest.mark.parametrize(
        "raw",
        ["Bad Name: v", "X-A\tB: v", "X-(A): v", "X-Ünicode: v", "X-A;B: v", '"X-A": v'],
    )
    def test_a_name_http_cannot_send_is_dropped(self, raw):
        """The promise is about the LLM CALL surviving, not about this function returning.

        `Bad Name` used to parse cleanly here and then die in h11 when the request was serialised, so
        every model call in an unattended run failed -- the exact outcome "degrade to no extra
        headers" exists to prevent. The good headers on other lines are kept.
        """
        assert parse_custom_headers(raw) == {}
        assert parse_custom_headers(f"{raw}\nX-Good: keep") == {"X-Good": "keep"}

    def test_a_value_http_cannot_send_is_dropped(self):
        assert parse_custom_headers("X-A: bad\x00value\nX-Good: keep") == {"X-Good": "keep"}

    def test_matches_the_bench_side_twin(self):
        """This helper is deliberately duplicated (appsci_agent must not import appsci_bench -- its
        image doesn't ship it). Pin the two copies to the same behaviour so they can't drift."""
        from appsci_bench.common.model_config import parse_custom_headers as bench_parse

        for raw in (
            "X-A: 1\nX-B: 2", "X-A: https://h:1/v", "junk", "", "  ", None, ": v", "X-A:",
            "Bad Name: v", "X-A\tB: v", "X-Ünicode: v", "X-A: nul\x00here",
            "Bad Name: v\nX-Good: keep",
        ):
            assert parse_custom_headers(raw) == bench_parse(raw), raw


class TestArtifactCredentialEnvVars:
    """`appsci_agent.core.model_access` mirrors the deployer's credential list, and says so in the file.

    Nothing pinned that instruction, and it drifted the first time it was tested: this PR added the two
    Fireworks names to the deployer and missed the mirror, so a Fireworks run would have told the agent
    nothing about model access while its artifact did receive the key.
    """

    def test_mirrors_the_deployers_credential_list_exactly(self):
        from appsci_agent.core.model_access import ARTIFACT_CREDENTIAL_ENV_VARS
        from appsci_bench.orchestrator.deployer import LLM_CREDENTIAL_ENV_VARS

        # Sets, not sequences: order is presentation, membership is the contract. A name in the
        # deployer but not here is a credential the artifact gets and the agent is never told about;
        # a name here but not there is a credential the agent is promised and never receives.
        assert set(ARTIFACT_CREDENTIAL_ENV_VARS) == set(LLM_CREDENTIAL_ENV_VARS)


class TestTalkToUserTool:
    """Tests for the talk_to_user tool."""

    def test_tool_metadata(self):
        """Test tool name and description."""
        tool = TalkToUserTool()
        assert tool.name == "talk_to_user"
        assert "user" in tool.description.lower()
        assert "question" in tool.parameters["properties"]

    def test_execute_without_env_vars(self):
        """Test execution when APPSCI_USERSIM_URL/TOKEN env vars are not set."""
        tool = TalkToUserTool()
        context = ToolContext()

        # Ensure env vars are not set
        with patch.dict("os.environ", {}, clear=True):
            result = tool.execute({"question": "What is the requirement?"}, context)

        assert not result.success
        # One message covers both "never reachable" and "answered with an error", because the client
        # now carries the service's own reason and this branch no longer has to guess which it was.
        assert "Could not get an answer from the stakeholder" in result.error
        assert "APPSCI_USERSIM_URL not set" in result.error

    def test_execute_with_user_sim_service(self):
        """Test execution with appsci agent user sim service via env vars."""
        tool = TalkToUserTool()
        context = ToolContext()

        # Mock the AppsciAgentUserSimClient
        mock_client = MagicMock()
        mock_client.ask.return_value = "The requirement is X"
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("appsci_agent.tools.talk_to_user.AppsciAgentUserSimClient", return_value=mock_client):
            result = tool.execute({"question": "What is the requirement?"}, context)

        assert result.success
        assert result.output["answer"] == "The requirement is X"
        assert result.output["question_asked"] == "What is the requirement?"
        mock_client.ask.assert_called_once_with("What is the requirement?", parent_span_id=None)

    def test_execute_updates_conversation_history(self):
        """Test that execution updates the agent's conversation history."""
        tool = TalkToUserTool()
        context = ToolContext()

        mock_client = MagicMock()
        mock_client.ask.return_value = "The answer is Y"
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("appsci_agent.tools.talk_to_user.AppsciAgentUserSimClient", return_value=mock_client):
            result = tool.execute({"question": "What is Y?"}, context)

        assert result.success
        assert len(context.conversation_history) == 2
        assert context.conversation_history[0] == {"role": "agent", "content": "What is Y?"}
        assert context.conversation_history[1] == {"role": "user_sim", "content": "The answer is Y"}

    def test_execute_with_context_parameter(self):
        """Test execution with optional context parameter."""
        tool = TalkToUserTool()
        context = ToolContext()

        mock_client = MagicMock()
        mock_client.ask.return_value = "Got it"
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("appsci_agent.tools.talk_to_user.AppsciAgentUserSimClient", return_value=mock_client):
            result = tool.execute({
                "question": "What format?",
                "context": "I need to know for the output"
            }, context)

        assert result.success
        # Verify the full question was sent with context
        mock_client.ask.assert_called_once_with("What format?\n\nContext: I need to know for the output", parent_span_id=None)

    def test_execute_missing_question(self):
        """Test execution with missing question parameter."""
        tool = TalkToUserTool()
        context = ToolContext()
        result = tool.execute({}, context)
        assert not result.success
        assert "required" in result.error.lower()

    def test_execute_handles_service_error(self):
        """Test execution handles errors from the appsci agent user sim service."""
        tool = TalkToUserTool()
        context = ToolContext()

        mock_client = MagicMock()
        mock_client.ask.side_effect = Exception("Connection refused")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("appsci_agent.tools.talk_to_user.AppsciAgentUserSimClient", return_value=mock_client):
            result = tool.execute({"question": "Test?"}, context)

        assert not result.success
        assert "Error communicating with user" in result.error
        assert result.output["question_asked"] == "Test?"


class TestCompleteTool:
    """Tests for the complete tool."""

    def test_tool_metadata(self):
        """The tool takes no arguments: evaluation runs against whatever deploy_artifact deployed."""
        tool = CompleteTool()
        assert tool.name == "complete"
        assert tool.parameters["properties"] == {}
        assert "endpoint_url" not in tool.parameters["properties"]
        assert not tool.parameters.get("required")

    def test_description_points_to_deploy_artifact_and_no_args(self):
        """The description must steer the agent to deploy via deploy_artifact and make clear complete
        takes no arguments -- the endpoint is whatever deploy_artifact recorded, not agent-authored."""
        desc = CompleteTool().description.lower()
        assert "deploy_artifact" in desc
        assert "no arguments" in desc

    def test_execute_no_deployed_endpoint(self):
        """With no deployed endpoint on the context, complete must fail and tell the agent to deploy."""
        tool = CompleteTool()
        context = ToolContext()
        result = tool.execute({}, context)
        assert not result.success
        assert "deploy_artifact" in result.error

    def test_execute_already_completed(self):
        """Test execution when the build was already completed."""
        tool = CompleteTool()
        context = ToolContext(
            artifact_endpoint="http://appsci-artifact-t:8080/respond",
            build_complete=True,
        )
        result = tool.execute({}, context)
        assert not result.success
        assert "already" in result.error.lower()

    def test_execute_success_from_context_endpoint(self, monkeypatch):
        """Happy path: complete reads the deployed endpoint from the context, verifies reachability,
        and marks the build complete (no orchestrator URL configured -> no POST)."""
        monkeypatch.delenv("ORCHESTRATOR_SUBMIT_URL", raising=False)
        tool = CompleteTool()
        monkeypatch.setattr(tool, "_check_endpoint", lambda url: (True, None))
        context = ToolContext(artifact_endpoint="http://appsci-artifact-t:8080/respond")
        result = tool.execute({}, context)
        assert result.success
        assert result.output["endpoint_url"] == "http://appsci-artifact-t:8080/respond"
        assert context.build_complete is True

    def test_execute_unreachable_endpoint_fails(self, monkeypatch):
        """If the deployed endpoint is no longer reachable, complete fails with the reachability error."""
        monkeypatch.delenv("ORCHESTRATOR_SUBMIT_URL", raising=False)
        tool = CompleteTool()
        monkeypatch.setattr(tool, "_check_endpoint", lambda url: (False, "Connection refused"))
        context = ToolContext(artifact_endpoint="http://appsci-artifact-t:8080/respond")
        result = tool.execute({}, context)
        assert not result.success
        assert "not reachable" in result.error.lower()
        assert context.build_complete is False


class TestArtifactBuilder:
    """Tests for the ArtifactBuilder."""

    def test_ensure_directories(self):
        """Test workspace and artifact directory creation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ArtifactBuilder(
                workspace_dir=Path(tmpdir) / "workspace",
                artifact_dir=Path(tmpdir) / "workspace" / "artifact",
            )
            
            workspace = builder.ensure_workspace()
            assert workspace.exists()
            
            artifact_dir = builder.ensure_artifact_dir()
            assert artifact_dir.exists()

    def test_validate_empty_artifact(self):
        """Test validation of empty artifact directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ArtifactBuilder(artifact_dir=Path(tmpdir) / "artifact")
            result = builder.validate_artifact()
            
            assert not result.is_valid
            assert any("not exist" in err for err in result.errors)

    def test_validate_missing_dockerfile(self):
        """Test validation when Dockerfile is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
            (artifact_dir / "server.py").write_text("# server code")
            
            builder = ArtifactBuilder(artifact_dir=artifact_dir)
            result = builder.validate_artifact()
            
            assert not result.is_valid
            assert any("Dockerfile" in err for err in result.errors)

    def test_validate_valid_artifact(self):
        """Test validation of a valid artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
            
            # Create required files
            (artifact_dir / "Dockerfile").write_text("""
FROM python:3.11-slim
WORKDIR /app
COPY . .
CMD ["python", "server.py"]
""")
            (artifact_dir / "server.py").write_text("# server code")
            (artifact_dir / "requirements.txt").write_text("fastapi")
            
            builder = ArtifactBuilder(artifact_dir=artifact_dir)
            result = builder.validate_artifact()
            
            assert result.is_valid
            assert len(result.errors) == 0
            assert "Dockerfile" in result.files_found



class TestPromptBuilder:
    """Tests for the PromptBuilder."""

    def test_build_system_prompt_basic(self):
        """Test basic system prompt generation."""
        prompt = build_system_prompt(
            task_brief="Build an invoice review agent",
        )
        
        assert "Applied AI Scientist" in prompt
        assert "invoice review" in prompt.lower()

    def test_build_system_prompt_with_environment(self):
        """Test system prompt with environment resources."""
        prompt = build_system_prompt(
            task_brief="Build an agent",
            environment={
                "resources": [
                    {"type": "database", "location": "postgres://localhost/db", "description": "Main database"},
                    {"type": "docs", "location": "/data/docs", "description": "Documentation"},
                ]
            },
        )
        
        assert "database" in prompt.lower()
        assert "postgres" in prompt.lower()

    def test_build_system_prompt_names_resources_when_locations_are_redacted(self):
        """The spec the agent container mounts is redacted: resources keep name/type/description and
        lose their host location. The prompt must identify each resource by the name the personas use
        instead of printing a placeholder for the stripped location."""
        prompt = build_system_prompt(
            task_brief="Build an agent",
            environment={
                "resources": [
                    {"name": "oltp_db", "type": "database", "description": "Main database"},
                    {"name": "contract_terms", "type": "docs", "description": "Documentation"},
                ]
            },
        )

        assert "oltp_db" in prompt and "contract_terms" in prompt
        assert "N/A" not in prompt
        # Each description stays on its own line rather than running into the next bullet.
        assert "  - Main database\n" in prompt

    def test_build_system_prompt_with_budgets(self):
        """Test system prompt with budget constraints."""
        prompt = build_system_prompt(
            task_brief="Build an agent",
            budgets={
                "build": {
                    "turns": 40,
                    "tokens": 2000000,
                    "usd": 20,
                    "wall_clock_s": 7200,
                },
                "latency_slo_ms": 2000,
            },
        )
        
        assert "40" in prompt  # turns
        assert "2000" in prompt or "2,000" in prompt  # latency SLO


class TestSystemPromptFailsLoud:
    """A system prompt that can't be built must abort the run, never fall back.

    The system prompt is what tells the agent its tool set and the deploy/submit contract, so
    substituting a different prompt would silently run the whole BUILD phase against instructions
    nobody reviewed -- and it would surface as a mysteriously poor artifact score rather than as the
    template bug it is. There used to be a `prompts/default_prompt.txt` fallback here; it was
    unreachable in practice (only an `except Exception` path), untested, and hand-synced with the
    Jinja template, so it was removed in favour of failing fast.
    """

    def _agent(self):
        from appsci_agent.core.agent import AppSciAgent
        return AppSciAgent(config=AgentConfig(model="test-model"))

    def test_render_failure_propagates(self):
        agent = self._agent()
        with patch.object(
            agent.prompt_builder, "build_system_prompt",
            side_effect=RuntimeError("template exploded"),
        ):
            with pytest.raises(RuntimeError, match="template exploded"):
                agent._build_system_prompt(task_description="build a thing")

    def test_no_default_prompt_fallback_remains(self):
        agent = self._agent()
        assert not hasattr(agent, "_default_system_prompt")
        prompts_dir = Path(__file__).resolve().parents[1] / "appsci_agent" / "prompts"
        assert not (prompts_dir / "default_prompt.txt").exists()

    def test_explicit_system_prompt_still_short_circuits(self):
        """An explicitly supplied prompt is a deliberate override, not a fallback -- keep it working
        (and it must not even consult the template)."""
        from appsci_agent.core.agent import AppSciAgent
        agent = AppSciAgent(
            config=AgentConfig(model="test-model"), system_prompt="EXPLICIT",
        )
        with patch.object(
            agent.prompt_builder, "build_system_prompt", side_effect=AssertionError("must not render"),
        ):
            assert agent._build_system_prompt(task_description="anything") == "EXPLICIT"

    def test_real_template_still_renders(self):
        """Guards the inverse risk: with the fallback gone, a broken template now breaks every run."""
        agent = self._agent()
        prompt = agent._build_system_prompt(
            task_description="Build an invoice review agent",
            environment={"resources": [{"name": "db", "type": "database", "description": "orders"}]},
            budgets={"build": {"turns": 40}},
        )
        assert "deploy_artifact" in prompt and "complete" in prompt


class TestToolContext:
    """Tests for ToolContext."""

    def test_default_context(self):
        """Test default context values."""
        context = ToolContext()
        assert context.workspace_dir == "/workspace"
        assert context.artifact_dir == "/workspace/artifact"
        assert not context.build_complete
        assert context.conversation_history == []

    def test_context_with_values(self):
        """Test context with custom values."""
        context = ToolContext(
            workspace_dir="/custom/workspace",
            environment={"key": "value"},
            conversation_history=[{"role": "agent", "content": "test"}],
        )

        assert context.workspace_dir == "/custom/workspace"
        assert context.environment["key"] == "value"
        assert len(context.conversation_history) == 1


class TestToolResult:
    """Tests for ToolResult."""

    def test_successful_result(self):
        """Test successful tool result."""
        result = ToolResult(
            success=True,
            output={"data": "value"},
        )
        
        assert result.success
        assert result.to_dict() == {"data": "value"}

    def test_failed_result(self):
        """Test failed tool result."""
        result = ToolResult(
            success=False,
            output={"partial": "data"},
            error="Something went wrong",
        )
        
        assert not result.success
        d = result.to_dict()
        assert d["error"] == "Something went wrong"
        assert d["partial"] == "data"


class TestLLMClientTimeout:
    """The agent's litellm calls must carry an explicit request timeout so a hung/throttled
    gateway request fails fast (and is retried) rather than parking on litellm's 6000s default."""

    def _fake_response(self):
        """Minimal object matching what LLMClient._parse_response reads."""
        message = MagicMock()
        message.content = "ok"
        message.tool_calls = None
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = None
        return response

    def test_chat_passes_default_timeout(self, monkeypatch):
        from appsci_agent.core.llm_client import LLMClient

        monkeypatch.delenv("APPSCI_LLM_TIMEOUT_S", raising=False)
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return self._fake_response()

        monkeypatch.setattr("appsci_agent.core.llm_client.litellm.completion", fake_completion)

        client = LLMClient(AgentConfig())
        client.chat([{"role": "user", "content": "hi"}])

        assert captured["timeout"] == 120.0

    def test_chat_timeout_env_override(self, monkeypatch):
        from appsci_agent.core.llm_client import LLMClient

        monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", "45")
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return self._fake_response()

        monkeypatch.setattr("appsci_agent.core.llm_client.litellm.completion", fake_completion)

        client = LLMClient(AgentConfig())
        client.chat([{"role": "user", "content": "hi"}])

        assert captured["timeout"] == 45.0

    def test_chat_explicit_kwarg_wins_over_env(self, monkeypatch):
        from appsci_agent.core.llm_client import LLMClient

        monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", "45")
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return self._fake_response()

        monkeypatch.setattr("appsci_agent.core.llm_client.litellm.completion", fake_completion)

        client = LLMClient(AgentConfig())
        client.chat([{"role": "user", "content": "hi"}], timeout=10)

        assert captured["timeout"] == 10


class TestLLMClientCustomHeaders:
    """Gateway identity headers (e.g. X-Gateway-User) must reach litellm as `extra_headers` -- the
    gateway can reject a request that omits them (403 identity_required) -- and must be ABSENT for
    anyone not using a gateway, so provider behaviour there is unchanged."""

    def _fake_response(self):
        message = MagicMock()
        message.content = "ok"
        message.tool_calls = None
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = None
        return response

    def _capture(self, monkeypatch, config):
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return self._fake_response()

        monkeypatch.setattr("appsci_agent.core.llm_client.litellm.completion", fake_completion)
        from appsci_agent.core.llm_client import LLMClient
        LLMClient(config).chat([{"role": "user", "content": "hi"}])
        return captured

    def test_chat_forwards_custom_headers(self, monkeypatch):
        cfg = AgentConfig(custom_headers={"X-Gateway-User": "a@b.com"})
        assert self._capture(monkeypatch, cfg)["extra_headers"] == {"X-Gateway-User": "a@b.com"}

    def test_chat_omits_extra_headers_when_unset(self, monkeypatch):
        assert "extra_headers" not in self._capture(monkeypatch, AgentConfig())

    def test_chat_omits_extra_headers_when_empty(self, monkeypatch):
        # Empty dict => omit the parameter entirely, not send `extra_headers={}`.
        captured = self._capture(monkeypatch, AgentConfig(custom_headers={}))
        assert "extra_headers" not in captured
