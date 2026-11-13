"""mini_swe_agent argument handling + prompt/registry consistency.

Both guards here come from a real run (`airline_rebooking_20260803_172329`, turn 4): the model
emitted a `mini_swe_agent` call carrying only `context` -- valid, complete JSON with the entire
build brief in the wrong field -- and the tool rejected it with a message so terse
(`"Task description is required"`) that recovery depended on the model inferring the remedy. It
did recover, but the turn cost 27s and 19k tokens.

The call was invalid and is still rejected; what changed is that the rejection now names the
missing field, names the field the text was misfiled into, and states how to re-send. The tool
schema does mark `task` required, but the function definition carries no `strict` flag and the
provider does not validate tool arguments, so invalid calls can always arrive -- the rejection
message is the only recovery channel.
"""

from appsci_agent.core.agent import AppSciAgent
from appsci_agent.core.config import AgentConfig
from appsci_agent.tools.mini_swe_agent import MiniSweAgentTool, resolve_task_args


class TestResolveTaskArgs:
    def test_task_and_context_pass_through(self):
        task, ctx, err = resolve_task_args({"task": "build the server", "context": "notes"})
        assert (task, ctx, err) == ("build the server", "notes", None)

    def test_task_alone_is_valid(self):
        task, ctx, err = resolve_task_args({"task": "build the server"})
        assert (task, ctx, err) == ("build the server", "", None)

    def test_context_only_is_rejected_not_repaired(self):
        """The observed failure mode: whole brief in `context`, no `task`.

        Rejected on purpose. Promoting `context` would guess at intent and would erase the
        malformed call from the trace.
        """
        task, ctx, err = resolve_task_args({"context": "Build an airline agent in /work/out"})
        assert (task, ctx) == ("", "")
        assert err is not None

    def test_context_only_error_is_actionable(self):
        """The rejection has to tell the model precisely how to re-send."""
        brief = "Build an airline agent in /work/out"
        _task, _ctx, err = resolve_task_args({"context": brief})
        assert "'task'" in err  # names the missing field
        assert "'context'" in err  # names the field it was misfiled into
        assert str(len(brief)) in err  # quantifies what it sent, so the diagnosis is unambiguous
        assert "re-send" in err.lower()  # states the remedy

    def test_neither_field_error_names_the_field(self):
        task, ctx, err = resolve_task_args({})
        assert (task, ctx) == ("", "")
        assert err is not None
        assert "'task'" in err
        assert "re-send" in err.lower()
        assert "neither" in err.lower()

    def test_whitespace_only_task_is_not_a_task(self):
        _task, _ctx, err = resolve_task_args({"task": "   \n ", "context": "the real brief"})
        assert err is not None

    def test_blank_everything_errors(self):
        _task, _ctx, err = resolve_task_args({"task": "  ", "context": "\n"})
        assert err is not None
        assert "neither" in err.lower()


class TestSchemaStillDeclaresTaskRequired:
    def test_required_task_is_advertised(self):
        """Salvaging bad calls must not silently drop the schema contract."""
        fn = MiniSweAgentTool().to_openai_tool()["function"]
        assert fn["parameters"]["required"] == ["task"]


class TestPromptRegistryConsistency:
    def test_every_registered_tool_is_advertised_on_both_surfaces(self):
        """A registered tool that no surface mentions can never be called by the model.

        Guards both surfaces at once: the Jinja system prompt and the code-built task message.
        """
        agent = AppSciAgent.__new__(AppSciAgent)  # no LLM client needed
        registered = {
            "talk_to_user",
            "mini_swe_agent",
            "deploy_artifact",
            "eval_generator",
            "optimize_agent",
            "complete",
        }
        task_message = AppSciAgent._format_task_message(
            agent, "Build an agent.", environment=None
        )
        from pathlib import Path

        import appsci_agent

        template = (
            Path(appsci_agent.__file__).parent / "prompts" / "system_prompt.jinja2"
        ).read_text()

        # Strip Jinja comments -- they're never rendered into the prompt.
        import re

        rendered_template = re.sub(r"\{#.*?#\}", "", template, flags=re.DOTALL)

        for surface_name, surface in (
            ("_format_task_message", task_message),
            ("system_prompt.jinja2", rendered_template),
        ):
            for tool in registered:
                assert tool in surface, (
                    f"{surface_name} does not advertise {tool}, which IS in the tool registry -- "
                    "the model will never know to call it. Add a section/mention for it there."
                )


class TestMiniSweModelKwargs:
    """Auth/transport knobs the tool injects into mini-swe's LitellmModel at runtime.

    mini-swe makes its own LLM calls (the bulk of a run's spend), so anything the agent's own client
    needs to reach the gateway must be injected here too -- notably the gateway identity headers
    (X-Gateway-User), which the gateway can require (403 identity_required). They must be ABSENT when
    unconfigured, so a first-party/no-gateway run's requests are unchanged.
    """

    def _model_args(self, monkeypatch, config):
        import appsci_agent.tools.mini_swe_agent as msa
        from appsci_agent.tools.base import ToolContext

        captured: dict = {}

        class _FakeModel:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _FakeAgent:
            def __init__(self, **kwargs):
                pass

            def run(self, task):
                return {"exit_status": "Submitted", "submission": "done"}

        class _FakeEnv:
            command_errors: list = []

            def __init__(self, **kwargs):
                pass

        # No publisher -> the tool takes the plain LitellmModel / SubAgentEnvironment path.
        monkeypatch.setattr(msa, "get_publisher", lambda: None)
        monkeypatch.setattr(msa, "LitellmModel", _FakeModel)
        monkeypatch.setattr(msa, "DefaultAgent", _FakeAgent)
        monkeypatch.setattr(msa, "SubAgentEnvironment", _FakeEnv)

        result = msa.MiniSweAgentTool().execute({"task": "do the thing"},
                                                ToolContext(config=config))
        assert result.success, result.error
        return captured

    def _model_kwargs(self, monkeypatch, config):
        return self._model_args(monkeypatch, config)["model_kwargs"]

    def test_custom_headers_reach_model_kwargs(self, monkeypatch):
        cfg = AgentConfig(custom_headers={"X-Gateway-User": "a@b.com"})
        assert self._model_kwargs(monkeypatch, cfg)["extra_headers"] == {
            "X-Gateway-User": "a@b.com"
        }

    def test_no_extra_headers_when_unconfigured(self, monkeypatch):
        kwargs = self._model_kwargs(monkeypatch, AgentConfig())
        assert "extra_headers" not in kwargs
        # The timeout injection (the other runtime knob on this path) still happens.
        assert kwargs["timeout"] == 120.0

    def test_sub_agent_runs_the_agents_own_model(self, monkeypatch):
        """The model reaching mini-swe is the one under test, not a second hardcoded id.

        This is the end of the path a sweep drives: swapping the agent's model has to move the
        sub-agent doing the implementation work, or a model comparison measures mostly the same
        model in every cell.
        """
        args = self._model_args(monkeypatch, AgentConfig(model="claude-haiku-4-5"))
        assert args["model_name"] == "claude-haiku-4-5"

    def test_sub_agent_model_can_be_pinned_apart_from_the_agent(self, monkeypatch):
        args = self._model_args(
            monkeypatch, AgentConfig(model="claude-haiku-4-5", mini_swe_model="claude-opus-4-8")
        )
        assert args["model_name"] == "claude-opus-4-8"

    def test_temperature_reaches_the_sub_agent_when_set(self, monkeypatch):
        """mini-swe is where the build-side temperature has to land.

        It does the implementation work and was 99.7% of the first smoke sweep's token use, so a
        temperature applied only to the interviewing agent would barely change what a rebuild
        produces -- and `builds: n` replicates would then differ by almost nothing while being
        reported as samples of a distribution.
        """
        kwargs = self._model_kwargs(monkeypatch, AgentConfig(temperature=1.0))
        assert kwargs["temperature"] == 1.0

    def test_temperature_zero_is_sent_rather_than_dropped(self, monkeypatch):
        """`0.0` is a real request -- sample as tightly as the provider will -- and truthiness anywhere
        on this path would silently drop it and run the build at the provider default while the cell
        record claimed 0.0.
        """
        kwargs = self._model_kwargs(monkeypatch, AgentConfig(temperature=0.0))
        assert kwargs["temperature"] == 0.0

    def test_no_temperature_is_sent_when_unset(self, monkeypatch):
        """The compatibility guarantee. Every run before this knob existed sent no temperature at all,
        so an unset config must leave the request byte-identical -- otherwise landing this feature
        would quietly change the conditions of every future build relative to the airline and gradient
        numbers already collected.
        """
        assert "temperature" not in self._model_kwargs(monkeypatch, AgentConfig())
        assert AgentConfig().temperature is None
