"""Does the builder's own (internal) eval see the changes the real (external) eval sees?

From the internal-vs-external analysis (scripts/analysis/internal_eval_consistency/results/<model>/
<task>/self_grade_vs_real/stats.json), Claude Opus 4.8 only. During a build the agent deploys a
program, grades it on an eval it wrote itself, changes it, and deploys again; afterwards every
deploy was re-scored on the held-out eval. The unit is one STEP: a move to a program the build had
not deployed before (the upstream `decisions`; putting an earlier program back is not a new step,
since it is the same comparison seen from the other side). Each build's internal exam is its own,
so a step only ever compares two programs of the same build.

A step is a real change when the external score rose or fell by at least one standard deviation of the
external measurement (the upstream "within noise" rule). Each step lands in one of five groups
(data.classify_step):
  agreed             real change, and the internal score moved the same way;
  disagreed          real change, and the internal score moved the other way;
  missed             real change (up or down), and the internal score did not move;
  false improvement  no real change, but the internal score went up: the agent's own exam
                     rewarded a change that did nothing;
  internal flat/drop no real change, and the internal score stayed flat or fell.
The headline is agreed / real changes, a disagreed or missed change counting against it. This is
directional agreement over consecutive steps, not Kendall's tau (which compares every pair of
checkpoints), so it is reported as a share.

Two figures, to sit side by side:
  internal_external_by_task  one 100% bar per task, its steps pooled over the task's builds; right
                             of the bar, the number of steps, agreed / real changes, and that share.
                             Tasks rest on very different numbers of steps, which the steps column
                             shows. The one-row legend under it serves the donut too.
  internal_external_overall  a donut of every step over all tasks, agreed / real changes inside.

No titles (the caption says what these are).

    python scripts/paper_plots/plot_internal_external.py

writes figures/internal_external/internal_external_{by_task,overall}.*.
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style  # noqa: E402
from data import (  # noqa: E402
    STEP_AGREED,
    STEP_DISAGREED,
    STEP_FALSE_UP,
    STEP_MISSED,
    STEP_NO_CHANGE,
    TASKS,
    TASKS_SHORT,
    Steps,
    load_steps,
)

OUT_DIR = style.FIGURES_DIR / "internal_external"
BY_TASK_NAME, OVERALL_NAME = "internal_external_by_task", "internal_external_overall"
HEIGHT = 160
BY_TASK_WIDTH, OVERALL_WIDTH = 338, 180  # 0.64 and 0.34 of the text width
BY_TASK_MARGIN = {"l": 62, "r": 100, "t": 14, "b": 42}  # b: x axis, then one legend row
FONT = style.TINY_FONT_SIZE

# Drawn left to right (and clockwise) in this order: the real changes first, then the rest.
GROUPS = (
    (STEP_AGREED, "Agreed", "#2E9C88"),
    (STEP_DISAGREED, "Disagreed", "#C65A48"),
    (STEP_MISSED, "Missed", "#8C7CCB"),
    (STEP_FALSE_UP, "False improvement", "#E0A030"),
    (STEP_NO_CHANGE, "Internal flat/drop", "#DAD6CE"),  # no real change, self-grade did not rise
)


def agreed_text(s: Steps) -> str:
    return f"{s.counts[STEP_AGREED]}/{s.real}" if s.real else "n/a"


def agreement_text(s: Steps) -> str:
    return "n/a" if s.agreement is None else style.pct(s.agreement)


def _legend(fig: go.Figure) -> None:
    """One row of swatch + name under the x axis title, from the figure's left edge, in px. It
    serves the donut beside this figure too."""
    plot_w = BY_TASK_WIDTH - BY_TASK_MARGIN["l"] - BY_TASK_MARGIN["r"]
    plot_h = HEIGHT - BY_TASK_MARGIN["t"] - BY_TASK_MARGIN["b"]
    y = BY_TASK_MARGIN["b"] - 7  # px below the plot area
    x0 = -BY_TASK_MARGIN["l"] + 8
    for _, name, color in GROUPS:
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=x0 / plot_w,
                      x1=(x0 + 7) / plot_w, y0=-(y + 3.5) / plot_h, y1=-(y - 3.5) / plot_h,
                      fillcolor=color, opacity=1, line={"width": 0})
        fig.add_annotation(x=(x0 + 10) / plot_w, y=-y / plot_h, xref="paper", yref="paper",
                           xanchor="left", yanchor="middle", showarrow=False, text=name,
                           font={"size": FONT - 1, "color": style.TEXT_PRIMARY})
        x0 += 10 + 3.9 * len(name) + 9  # swatch, name at about 3.9 px a character, gap


def by_task_figure(steps: dict[str, Steps]) -> go.Figure:
    if not steps:
        raise ValueError("no task has a step to classify")
    # Best agreement on top, then more real changes; tasks with none at the bottom.
    tasks = sorted(steps, key=lambda t: (
        -(steps[t].counts[STEP_AGREED] / steps[t].real) if steps[t].real else 1, -steps[t].real))
    labels = [TASKS_SHORT[t] for t in tasks]
    fig = go.Figure()
    for group, name, color in GROUPS:
        fig.add_trace(go.Bar(
            x=[steps[t].share(group) for t in tasks], y=labels, orientation="h", name=name,
            marker={"color": color, "line": {"color": style.SURFACE, "width": 1}}, width=0.66,
            hovertext=[f"<b>{TASKS[t]}</b><br>{name}: {steps[t].counts[group]} of "
                       f"{steps[t].total} steps" for t in tasks],
            hoverinfo="text",
        ))

    # Three columns right of the bars, under their headers.
    columns = ((8, "steps", lambda s: f"{s.total}"),
               (36, "agreed", lambda s: f"<b>{agreed_text(s)}</b>" if s.real else "n/a"),
               (72, "%", agreement_text))
    for xshift, header, value in columns:
        fig.add_annotation(x=1, xref="paper", xanchor="left", xshift=xshift, y=-0.5, yref="y",
                           yanchor="bottom", showarrow=False, text=header,
                           font={"size": FONT - 1, "color": style.TEXT_SECONDARY})
        for label, t in zip(labels, tasks, strict=True):
            fig.add_annotation(x=1, xref="paper", xanchor="left", xshift=xshift, y=label,
                               yref="y", showarrow=False, text=value(steps[t]),
                               font={"size": FONT, "color": style.TEXT_PRIMARY})

    _legend(fig)
    style.apply_paper_layout(fig, width=BY_TASK_WIDTH, height=HEIGHT)
    fig.update_layout(barmode="stack", bargap=0.3, margin=BY_TASK_MARGIN, showlegend=False)
    fig.update_yaxes(autorange="reversed", showline=False, ticks="", ticksuffix=" ",
                     automargin=False, tickfont={"size": FONT, "color": style.TEXT_PRIMARY})
    fig.update_xaxes(range=[0, 1], tickvals=[0, 0.5, 1], tickformat=".0%", showgrid=False,
                     showline=False, ticks="", zeroline=False,
                     tickfont={"size": FONT - 1, "color": style.TEXT_SECONDARY},
                     title={"text": "Share of optimization steps", "standoff": 2,
                            "font": {"size": FONT - 1, "color": style.TEXT_SECONDARY}})
    return fig


def overall_figure(steps: dict[str, Steps]) -> go.Figure:
    total = sum(steps.values(), Steps())
    if not total.total:
        raise ValueError("no step to classify")
    fig = go.Figure(go.Pie(
        values=[total.counts[g] for g, _, _ in GROUPS],
        labels=[name for _, name, _ in GROUPS],
        marker={"colors": [c for _, _, c in GROUPS], "line": {"color": style.SURFACE, "width": 2}},
        hole=0.58, sort=False, direction="clockwise", rotation=0,
        texttemplate="%{value}", textposition="inside",
        textfont={"size": FONT, "color": style.TEXT_PRIMARY}, insidetextorientation="horizontal",
        hovertemplate="%{label}: %{value} of " + str(total.total) + " steps<extra></extra>",
    ))
    fig.add_annotation(x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                       text=(f"<b>{agreed_text(total)}</b><br><span style='font-size:"
                             f"{FONT - 1}px;color:{style.TEXT_SECONDARY}'>agreed</span>"),
                       font={"size": FONT + 4, "color": style.TEXT_PRIMARY})
    style.apply_paper_layout(fig, width=OVERALL_WIDTH, height=HEIGHT)
    # No legend of its own: the bars beside it carry one. Top and bottom margins leave the donut
    # spanning the bars and their axis.
    fig.update_layout(margin={"l": 2, "r": 2, "t": BY_TASK_MARGIN["t"], "b": 14},
                      showlegend=False)
    return fig


def main() -> None:
    steps = load_steps()
    for t, s in steps.items():
        counts = ", ".join(f"{name.split(' (')[0].lower()} {s.counts[g]}" for g, name, _ in GROUPS)
        print(f"  {TASKS[t]:24} {s.builds} builds, {s.total} steps: {counts}; "
              f"agreed {agreed_text(s)} ({agreement_text(s)})")
    for fig, name in ((by_task_figure(steps), BY_TASK_NAME), (overall_figure(steps), OVERALL_NAME)):
        for path in style.save_figure(fig, name, OUT_DIR):
            print(path)


if __name__ == "__main__":
    main()
