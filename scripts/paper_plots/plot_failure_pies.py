#!/usr/bin/env python3
"""Plot Type 1 and Type 2 subcategory shares from saved build classifications.

Type 1 counts every supported build/category pair; Type 2 keeps one primary
category per build. Pie shares use assignments, centers use unique builds. No
classification, experiment, download or model call runs; Type 3 is not plotted.

Run: python scripts/paper_plots/plot_failure_pies.py --overwrite
Images and audit CSV/JSON files default to scripts/paper_plots/figures/.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import math
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIGURES_DIR = Path(__file__).resolve().parent / "figures"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import failure_plot_style as style  # noqa: E402
from scripts.analyze_build_failure_reasons import (  # noqa: E402
    OUTPUT_DIR as REASONS_DIR,
)
from scripts.analyze_build_failure_reasons import (  # noqa: E402
    REASON_LABELS,
    REASONS_BY_CATEGORY,
    build_error_categories,
    load_reasons,
)
from scripts.analyze_build_failures import AuditError, atomic_write, csv_text, sha  # noqa: E402
from scripts.classify_build_artifacts import OUTPUT_DIR, UNKNOWN, load_classifications  # noqa: E402
from scripts.plot_failure_analysis import (  # noqa: E402
    BUILD_CATEGORIES,
    EARLY_CATEGORIES,
    LABELS,
    PLOT_CATEGORIES,
    condition_label,
)

PIES = {
    "type1": ("build_time_error", "Type 1 · Build errors", BUILD_CATEGORIES),
    "type2": ("early_artifact_error", "Type 2 · Artifact errors", EARLY_CATEGORIES),
}
FILENAMES = {"type1": "build_error_subcategories.png", "type2": "artifact_error_subcategories.png"}
SUBCATEGORY_LABELS = {
    c: label.replace("\n", " ") for c, label in zip(PLOT_CATEGORIES, LABELS, strict=True)
}
# Color denotes subcategory here, not model or task. Stable even with zero counts.
PALETTE = ("#6F8FD6", "#E0A030", "#C65A48", "#C372B4", "#1baf7a", "#3FA3CF")
BUILD_PALETTE = ("#4476bd", "#a66b00", "#b74d3d", "#97409b", "#008360")
UNMAPPED = "unmapped_subcategory"
OTHER_COLOR = "#4575b4"
OUTER_RADIUS = 1.0
INNER_RADIUS = 0.58
CENTER_RADIUS = 0.17
CALLOUT_FONT_SIZE = 11
TOOL_CATEGORIES = ("turn_limit_mini_swe", "turn_limit_deploy_artifact", "turn_limit_optimize_agent")
INLINE_PERCENT_REASONS = {
    "stale_deployment_guard",
    "container_unreachable",
    "parent_response_truncated",
}
# Display-only line breaks/short forms; audit exports retain the full labels.
WEDGE_LABELS = {
    "six_unusable_turns": "Six consecutive\nunusable turns",
    "response_truncated": "Response\ntruncated",
    "turn_limit_mini_swe": "mini-SWE\nfailures\n(40-turn limit\nexceeded)",
    "turn_limit_deploy_artifact": "Deploy artifact\nerrors\n(40-turn limit\nexceeded)",
    "turn_limit_optimize_agent": "Optimization\ntool errors\n(40-turn limit\nexceeded)",
    UNMAPPED: "Other",
    "subagent_LimitsExceeded": "LimitsExceeded\ntermination",
    "subagent_RepeatedFormatError": "RepeatedFormatError\ntermination",
    "mini_swe_format_error": "FormatError\nresponses",
    "eval_integrity_guard": "Evaluation\nintegrity\nguard",
    "missing_Dockerfile": "Missing\nDockerfile",
    "invalid_port": "Invalid port\nconfiguration",
    "missing_module_or_executable": "Missing module\nor executable",
    "stale_deployment_guard": "Stale\ndeployment\nguard",
    "container_unreachable": "Container\nunreachable",
    "parent_response_truncated": "Truncated\nparent\nresponse",
    "internal_eval_no_score": "Internal eval:\nno score",
    "optimization_saturated": "Saturation\nwithout\nfinalization",
    "context_window_exception": "Context window\nexceeded",
    "artifact_modified_after_deploy": "Artifact modified\nafter deployment",
}
COUNTS_FIELDS = (
    "failure_type",
    "outcome",
    "category_id",
    "label",
    "color",
    "n_builds",
    "denominator_builds",
    "denominator_assignments",
    "share_pct",
    "build_ids_json",
)


def prepare_data(
    classification_dir=OUTPUT_DIR, *, optimization="both", models=(), reasons_dir=None
):
    builds, source = load_classifications(
        classification_dir, optimization=optimization, models=models
    )
    counts, members, groups = [], [], {}
    for kind, (outcome, title, categories) in PIES.items():
        selected = [r for r in builds if r["outcome"] == outcome]
        assignments = collections.defaultdict(list)
        for row in selected:
            category = row["category_id"]
            if category not in (*categories, UNKNOWN):
                raise AuditError(
                    f"Primary category {category!r} contradicts {outcome}: {row['build_id']}"
                )
            labels = build_error_categories(row) if kind == "type1" else [category]
            for category in labels:
                category = UNMAPPED if category == UNKNOWN else category
                assignments[category].append(row["build_id"])
                members.append({"failure_type": kind, "pie_category_id": category, **row})
        denominator = sum(len(ids) for ids in assignments.values())
        displayed = (*categories, *((UNMAPPED,) if assignments[UNMAPPED] else ()))
        for i, category in enumerate(displayed):
            ids = sorted(assignments[category])
            counts.append(
                {
                    "failure_type": kind,
                    "outcome": outcome,
                    "category_id": category,
                    "label": "Outside listed subcategories / unresolved"
                    if category == UNMAPPED
                    else SUBCATEGORY_LABELS[category],
                    "color": (OTHER_COLOR if kind == "type1" else style.AXIS)
                    if category == UNMAPPED
                    else (BUILD_PALETTE if kind == "type1" else PALETTE)[i],
                    "n_builds": len(ids),
                    "denominator_builds": len(selected),
                    "denominator_assignments": denominator,
                    "share_pct": 100 * len(ids) / denominator if denominator else "",
                    "build_ids_json": json.dumps(ids),
                }
            )
        groups[kind] = {
            "outcome": outcome,
            "title": title,
            "total_builds": len(selected),
            "total_assignments": denominator,
            "mapped_builds": len(selected) - len(assignments[UNMAPPED]),
            "unmapped_build_ids": sorted(assignments[UNMAPPED]),
        }
    conditions = sorted({bool(int(r["optimization_enabled"])) for r in builds})
    meta = {
        "version": 2,
        "unit": "build_category",
        "selected_cohort_builds": len(builds),
        "plotted_builds": len({r["build_id"] for r in members}),
        "plotted_assignments": len(members),
        "conditions": conditions,
        "filters": {"optimization": optimization, "models": list(models)},
        "pies": groups,
        "primary_policy": source.get("primary_policy", "saved_primary_category"),
        "counting": "Type 1: one vote per supported build/category pair, including secondary categories. Type 2: one primary-category vote per build, unchanged. Repeated events, trials and samples never add votes. Type 3/4 and overall-unclassified builds are not plotted.",
        "denominator": "Shares use all category assignments of each type, including outside-category assignments, and sum to 100% before rounding. A build can contribute to multiple Type 1 categories but only once to any given category. Center counts unique failed builds / all selected builds. Zero-count categories remain in a note (Type 1) or legend (Type 2).",
        "interpretation": "Shares describe supported category assignments, not independent causal effects or overall failure rates across all builds.",
        "sources": source["sources"],
    }
    details = {}
    if reasons_dir is not None:
        details, detail_sources = load_reasons(reasons_dir, classification_dir)
        meta["sources"].extend(detail_sources)
        all_builds, _ = load_classifications(classification_dir)
        expected = {
            (b["build_id"], category)
            for b in all_builds
            if b["outcome"] == "build_time_error"
            for category in build_error_categories(b)
        }
        if set(details) != expected:
            raise AuditError(
                "Detailed reasons must cover exactly the saved Type 1 build/category pairs"
            )
    reason_counts = []
    for parent in (r for r in counts if r["failure_type"] == "type1"):
        category = parent["category_id"]
        original = UNKNOWN if category == UNMAPPED else category
        matching = [
            b for b in members if b["failure_type"] == "type1" and b["pie_category_id"] == category
        ]
        groups = collections.defaultdict(list)
        for build in matching:
            detail = details.get((build["build_id"], original), {})
            reason = detail.get("reason_id", "unresolved_detail")
            build["fine_reason_id"] = reason
            build["fine_reason_label"] = REASON_LABELS[reason]
            build["fine_reason_support_json"] = detail.get("all_reason_support_json", "{}")
            groups[reason].append(build["build_id"])
        ordered = [r for r in (*REASONS_BY_CATEGORY[original], "unresolved_detail") if groups[r]]
        # Larger sibling slices get darker tints; keep the diagnostic wedge order.
        # Stable sorting uses that same order to break equal-count ties.
        shade_order = sorted(ordered, key=lambda reason: -len(groups[reason]))
        shades = {
            reason: 0.5 + 0.24 * i / max(1, len(shade_order) - 1)
            for i, reason in enumerate(shade_order)
        }
        for reason in ordered:
            ids = sorted(groups[reason])
            shade = shades[reason]
            rgb = [int(parent["color"][j : j + 2], 16) for j in (1, 3, 5)]
            color = "#" + "".join(f"{round(c + (255 - c) * shade):02x}" for c in rgb)
            reason_counts.append(
                {
                    "category_id": category,
                    "category_label": parent["label"],
                    "reason_id": reason,
                    "label": REASON_LABELS[reason],
                    "color": color,
                    "n_builds": len(ids),
                    "denominator_builds": parent["denominator_builds"],
                    "denominator_assignments": parent["denominator_assignments"],
                    "share_pct": 100 * len(ids) / parent["denominator_assignments"],
                    "within_category_pct": 100 * len(ids) / parent["n_builds"],
                    "build_ids_json": json.dumps(ids),
                }
            )
    meta["type1_reason_counts"] = reason_counts
    meta["type1_display_category_counts"], display_reasons = group_build_display(
        counts, reason_counts
    )
    meta["type1_display_reason_counts"] = merge_adjacent_reasons(display_reasons)
    meta["type1_center"] = f"{meta['pies']['type1']['total_builds']}/{len(builds)}"
    meta["type1_color_rule"] = (
        "Within each parent category, larger detailed slices have darker tints. "
        "Equal-count ties follow diagnostic order; wedge order and counts do not change."
    )
    meta["type1_detail_rule"] = (
        "Outer ring uses the most-supported diagnostic per build/category pair. Adjacent slices with the same established reason are merged for display across parent boundaries; per-parent audit rows are preserved. Each build can appear in multiple categories, once per category, with one detailed diagnostic per assignment. Both rings use Type 1 category assignments as denominator. Center is unique Type 1 failed builds / all selected builds. No matching evidence is labeled Detail not established."
    )
    meta["type1_grouping"] = (
        "The inner wheel shows mini-SWE, deploy artifact, optimization tool, and Other. "
        "All other source categories are grouped into Other for display only. Original "
        "category assignments and detailed reasons remain in audit CSVs. Group sizes sum "
        "assignments, not deduplicated build IDs; center still counts unique failed builds."
    )
    return counts, members, meta


def group_build_display(counts, reasons):
    """Regroup the first wheel without dropping votes or modifying saved evidence."""
    source = [r for r in counts if r["failure_type"] == "type1"]
    parents, children = [], []
    for category in (*TOOL_CATEGORIES, UNMAPPED):
        originals = [
            r
            for r in source
            if (
                r["category_id"] not in TOOL_CATEGORIES
                if category == UNMAPPED
                else r["category_id"] == category
            )
        ]
        ids = sorted({bid for r in originals for bid in json.loads(r["build_ids_json"])})
        parent = {
            **(originals[0] if originals else {}),
            "category_id": category,
            "label": "Other" if category == UNMAPPED else SUBCATEGORY_LABELS[category],
            "color": OTHER_COLOR if category == UNMAPPED else originals[0]["color"],
            "n_builds": len(ids),
            "n_assignments": sum(r["n_builds"] for r in originals),
            "share_pct": sum(r["share_pct"] or 0 for r in originals),
            "build_ids_json": json.dumps(ids),
            "source_category_ids": [r["category_id"] for r in originals],
        }
        parents.append(parent)
        matching = [r for r in reasons if r["category_id"] in parent["source_category_ids"]]
        shades = {
            id(r): 0.5 + 0.24 * i / max(1, len(matching) - 1)
            for i, r in enumerate(sorted(matching, key=lambda r: -r["n_builds"]))
        }
        rgb = [int(parent["color"][j : j + 2], 16) for j in (1, 3, 5)]
        for row in matching:
            children.append(
                {
                    **row,
                    "display_category_id": category,
                    "color": "#"
                    + "".join(f"{round(c + (255 - c) * shades[id(row)]):02x}" for c in rgb),
                }
            )
    return parents, children


def merge_adjacent_reasons(rows):
    """Merge contiguous identical diagnostics without moving parent boundaries.

    Unknown details are not evidence of a shared reason. Slice size continues to
    count assignments, even if one build supports the reason under two parents.
    The original per-parent rows remain unchanged in the reasons audit export.
    """
    merged = []
    for row in rows:
        if (
            merged
            and row["reason_id"] != "unresolved_detail"
            and merged[-1]["reason_id"] == row["reason_id"]
        ):
            target = merged[-1]
            target["n_assignments"] += row["n_builds"]
            ids = sorted(
                set(json.loads(target["build_ids_json"])) | set(json.loads(row["build_ids_json"]))
            )
            target["build_ids_json"] = json.dumps(ids)
            target["n_builds"] = len(ids)
            target["share_pct"] += row["share_pct"]
            target["source_category_ids"].append(row["category_id"])
            target.pop("within_category_pct", None)
        else:
            merged.append(
                {
                    **row,
                    "n_assignments": row["n_builds"],
                    "source_category_ids": [row["category_id"]],
                }
            )
    return merged


def add_wedge_label(ax, wedge, row, *, inner):
    """Label sectors directly, rotating narrow labels along the radius.

    Fit the text's rotated rectangle inside its actual annular sector. Very
    small slices use a callout instead of unreadably small or clipped text.
    """
    key = row["category_id"] if inner else row["reason_id"]
    words = WEDGE_LABELS.get(key, textwrap.fill(row["label"], 18))
    inline = not inner and key in INLINE_PERCENT_REASONS
    percentage = f"{row['share_pct']:.1f}%"
    label = f"{words}{' ' if inline else chr(10)}{percentage}"
    variants = [label]
    if not inner:
        compact = words.replace("\n", " ")
        variants += [
            f"{textwrap.fill(compact, width)}{' ' if inline else chr(10)}{percentage}"
            for width in (14, 18, 24)
        ]
        variants = list(dict.fromkeys(variants))
    angle = (wedge.theta1 + wedge.theta2) / 2
    span = wedge.theta2 - wedge.theta1
    if inner and span < 20:
        short = "Truncated" if key == "response_truncated" else label.rsplit("\n", 1)[0]
        label = f"{short.replace(chr(10), ' ')} {row['share_pct']:.1f}%"
        variants = [label]
    radius = wedge.r - wedge.width / 2
    if inner and span < 45:
        radius = wedge.r - wedge.width * 0.36  # More room away from the tapered tip.
    radians = math.radians(angle)
    x, y = radius * math.cos(radians), radius * math.sin(radians)
    rotation = (angle + 90) % 180 - 90 if span < (45 if inner else 110) else 0
    text = ax.text(
        x,
        y,
        label,
        ha="center",
        va="center",
        rotation_mode="anchor",
        color=style.label_color(row["color"]),
        fontfamily=style.font_family(),
        fontweight="bold" if inner else "normal",
        linespacing=1.2,
    )
    renderer = ax.figure.canvas.get_renderer()
    center = ax.transData.transform((x, y))
    rot = math.radians(rotation)
    for size in range(18 if inner else 15, 7, -1):
        text.set_fontsize(size)
        for candidate in variants:
            text.set_text(candidate)
            bbox = text.get_window_extent(renderer)
            fits = True
            for dx in (-bbox.width / 2 - 2, bbox.width / 2 + 2):
                for dy in (-bbox.height / 2 - 2, bbox.height / 2 + 2):
                    px = center[0] + dx * math.cos(rot) - dy * math.sin(rot)
                    py = center[1] + dx * math.sin(rot) + dy * math.cos(rot)
                    tx, ty = ax.transData.inverted().transform((px, py))
                    distance = math.hypot(tx, ty)
                    delta = (math.degrees(math.atan2(ty, tx)) - angle + 180) % 360 - 180
                    if not (
                        wedge.r - wedge.width + 0.008 < distance < wedge.r - 0.008
                        and abs(delta) < span / 2 - 0.4
                    ):
                        fits = False
            if fits:
                text.set_rotation(rotation)
                text.set_gid("sunburst-wedge-label")
                return text
    text.remove()
    return ax.annotate(
        label,
        xy=(x, y) if inner else (wedge.r * math.cos(radians), wedge.r * math.sin(radians)),
        xytext=(1.08 * math.cos(radians), 1.08 * math.sin(radians)),
        ha="left" if x >= 0 else "right",
        va="center",
        fontsize=CALLOUT_FONT_SIZE,
        color=style.TEXT_PRIMARY,
        arrowprops={"arrowstyle": "-", "color": style.TEXT_SECONDARY, "lw": 0.7},
    )


def make_build_figure(counts, meta):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.text import Annotation

    rows = meta["type1_display_category_counts"]
    nonzero = [r for r in rows if r["n_builds"]]
    details = meta["type1_display_reason_counts"]
    fig = plt.figure(figsize=(8.5, 9))
    ax = fig.add_axes((0.025, 0.12, 0.95, 0.74))
    labels = []
    if nonzero:
        for inner, values, radius, width in (
            (True, nonzero, INNER_RADIUS, INNER_RADIUS - CENTER_RADIUS),
            (False, details, OUTER_RADIUS, OUTER_RADIUS - INNER_RADIUS),
        ):
            wedges, _ = ax.pie(
                [r.get("n_assignments", r["n_builds"]) for r in values],
                colors=[r["color"] for r in values],
                radius=radius,
                startangle=90,
                counterclock=False,
                wedgeprops={"width": width, "edgecolor": style.SURFACE, "linewidth": 1.8},
            )
            for wedge, row in zip(wedges, values, strict=True):
                if row.get("reason_id") == "unresolved_detail":
                    wedge.set_hatch("///")
                labels.append((inner, wedge, row))
    else:
        ax.text(
            0.5, 0.85, "No failures of this type", transform=ax.transAxes, ha="center", fontsize=13
        )
    ax.text(
        0, 0.025, meta["type1_center"], ha="center", va="center", fontsize=17, fontweight="bold"
    )
    ax.text(0, -0.055, "failed / total", ha="center", va="center", fontsize=8)
    ax.set_xlim(-1.04, 1.04)
    ax.set_ylim(-1.04, 1.04)
    ax.set_axis_off()
    fig.canvas.draw()  # Final aspect/transform before fitting the wedge labels.
    callouts = []
    for inner, wedge, row in labels:
        label = add_wedge_label(ax, wedge, row, inner=inner)
        if isinstance(label, Annotation) and label.get_position()[1] > 0.8:
            callouts.append(label)
    # Adjacent tiny slices near twelve o'clock otherwise put their external
    # labels on top of one another and the subtitle. Spread them horizontally
    # in slice order; keep their leader lines attached to the original wedges.
    callouts.sort(key=lambda label: label.xy[0])
    for index, label in enumerate(callouts):
        x = -0.9 + 1.8 * index / max(1, len(callouts) - 1)
        label.set_position((x if len(callouts) > 1 else 0, 1.07))
        label.set_ha("center")
        label.set_va("bottom")
        label.set_text("\n".join(textwrap.fill(line, 19) for line in label.get_text().splitlines()))
    fig.text(0.5, 0.97, "Build failures", ha="center", fontsize=18, fontweight="bold")
    fig.text(
        0.5,
        0.94,
        f"{condition_label(meta['conditions'])} · Inner: subcategory · Outer: detailed reason",
        ha="center",
        fontsize=10,
        color=style.TEXT_SECONDARY,
    )
    zero_labels = [row["label"] for row in rows if not row["n_builds"]]
    if zero_labels:
        fig.text(
            0.5,
            0.087,
            textwrap.fill("No supported assignments (0%): " + "; ".join(zero_labels), 105),
            ha="center",
            va="top",
            fontsize=8,
            color=style.TEXT_SECONDARY,
        )
    fig.text(
        0.5,
        0.025,
        f"{meta['pies']['type1']['total_assignments']} category assignments across "
        f"{meta['pies']['type1']['total_builds']} failed builds; a build can appear in multiple categories.\n"
        "Percentages: share of assignments. Center: unique failed / all analyzed builds.",
        ha="center",
        fontsize=8,
        color=style.TEXT_SECONDARY,
    )
    return style.apply_figure_style(fig)


def make_figure(kind, counts, meta):
    if kind == "type1":
        return make_build_figure(counts, meta)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    rows = [r for r in counts if r["failure_type"] == kind]
    group = meta["pies"][kind]
    nonzero = [r for r in rows if r["n_builds"]]
    fig = plt.figure(figsize=(11, 6))
    ax = fig.add_axes((0.035, 0.17, 0.48, 0.68))
    if nonzero:
        wedges, _, texts = ax.pie(
            [r["n_builds"] for r in nonzero],
            colors=[r["color"] for r in nonzero],
            startangle=90,
            counterclock=False,
            autopct=lambda pct: f"{pct:.1f}%" if pct >= 4 else "",
            pctdistance=0.7,
            wedgeprops={"linewidth": 1.5, "edgecolor": style.SURFACE},
            textprops={"fontsize": 13, "fontweight": "bold"},
        )
        for wedge, text, row in zip(wedges, texts, nonzero, strict=True):
            text.set_color(style.label_color(row["color"]))
            if row["category_id"] == UNMAPPED:
                wedge.set_hatch("///")
    else:
        ax.text(
            0.5,
            0.5,
            "No failures of this type\nin the selected cohort",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=14,
        )
    ax.set_axis_off()
    handles = []
    for row in rows:
        share = f"{row['share_pct']:.1f}%" if group["total_builds"] else "n/a"
        unit = "build" if row["n_builds"] == 1 else "builds"
        handles.append(
            Patch(
                facecolor=row["color"],
                hatch="///" if row["category_id"] == UNMAPPED else None,
                label=f"{row['label']}\n{row['n_builds']} {unit} · {share}",
            )
        )
    fig.legend(
        handles=handles,
        loc="center left",
        bbox_to_anchor=(0.51, 0.51),
        fontsize=10.5,
        labelspacing=1.1,
        frameon=False,
    )
    fig.text(0.045, 0.94, group["title"], fontsize=style.TITLE_SIZE, fontweight="bold")
    fig.text(
        0.045,
        0.895,
        f"{group['total_builds']} failures · {condition_label(meta['conditions'])} · all selected models/tasks",
        fontsize=11,
        color=style.TEXT_SECONDARY,
    )
    fig.text(
        0.045,
        0.08,
        "One vote per build, using its saved primary cause. Percentages are within this error type.\n"
        "Trials and secondary labels are evidence, not extra votes. Type 3 is not included.",
        fontsize=10,
        color=style.TEXT_SECONDARY,
        va="top",
    )
    if group["unmapped_build_ids"]:
        fig.text(
            0.045,
            0.02,
            "Grey hatched slice retains failures outside the listed subcategories; none are silently dropped.",
            fontsize=9.5,
            color=style.TEXT_SECONDARY,
        )
    return style.apply_figure_style(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--reasons-dir",
        type=Path,
        default=REASONS_DIR,
        help="Saved Type 1 detailed reasons from analyze_build_failure_reasons.py",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=FIGURES_DIR,
        help="Destination for both pie charts and their audit CSV/JSON files",
    )
    parser.add_argument("--optimization", choices=("both", "enabled", "disabled"), default="both")
    parser.add_argument("--model", action="append", default=[], help="Model filter; repeatable")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only the two images and four audit sidecars",
    )
    args = parser.parse_args(argv)
    try:
        counts, members, meta = prepare_data(
            args.classification_dir,
            optimization=args.optimization,
            models=args.model,
            reasons_dir=args.reasons_dir,
        )
        names = [
            *FILENAMES.values(),
            "failure_pies.counts.csv",
            "failure_pies.builds.csv",
            "failure_pies.meta.json",
            "failure_pies.reasons.csv",
        ]
        protected = {Path(s["path"]).resolve() for s in meta["sources"]}
        for name in names:
            path = args.output_dir / name
            if (
                path.is_symlink()
                or path.resolve() in protected
                or path.resolve().is_relative_to(args.classification_dir.resolve())
            ):
                raise AuditError(
                    f"Refusing to write into source classifications or a symlink: {path}"
                )
            if path.exists() and not args.overwrite:
                raise AuditError(
                    f"Output exists: {path}; pass --overwrite or choose another directory"
                )
        outputs = {}
        for kind, filename in FILENAMES.items():
            figure = make_figure(kind, counts, meta)
            import matplotlib.pyplot as plt

            try:
                buffer = io.BytesIO()
                figure.savefig(buffer, format="png", dpi=style.PNG_DPI)
                outputs[filename] = buffer.getvalue()
            finally:
                plt.close(figure)
        outputs[names[2]] = csv_text(counts, COUNTS_FIELDS).encode()
        outputs[names[3]] = csv_text(
            members, ("failure_type", "pie_category_id", "build_id") if not members else None
        ).encode()
        # Never publish plots if their saved classification inputs changed mid-render.
        for source in meta["sources"]:
            if sha(Path(source["path"])) != source["sha256"]:
                raise AuditError("Classifications changed while plotting; rerun")
        meta["plot_script"] = {"path": str(Path(__file__).resolve()), "sha256": sha(Path(__file__))}
        outputs[names[4]] = (json.dumps(meta, indent=2) + "\n").encode()
        outputs[names[5]] = csv_text(
            meta["type1_reason_counts"],
            None if meta["type1_reason_counts"] else ("category_id", "reason_id", "n_builds"),
        ).encode()
        for name, data in outputs.items():
            atomic_write(args.output_dir / name, data)
        for kind, filename in FILENAMES.items():
            print(f"{args.output_dir / filename}: {meta['pies'][kind]['total_builds']} builds")
        print(f"Audit sidecars: {args.output_dir / 'failure_pies.*'}; no classifications changed.")
        return 0
    except (AuditError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
