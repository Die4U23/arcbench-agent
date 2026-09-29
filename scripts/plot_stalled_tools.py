"""Draw the offline failing-tool replay comparison (matplotlib is optional)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scenarios = json.loads(args.data.read_text(encoding="utf-8"))["scenarios"]
    colors = {"before": "#dc2626", "after": "#059669"}
    figure, axes = plt.subplots(1, 2, figsize=(10, 3.8), constrained_layout=True)
    for label, data in scenarios.items():
        samples = data["samples"]
        axes[0].plot(
            [item["request"] for item in samples],
            [item["cumulative_prompt_chars"] / 1000 for item in samples],
            marker="o", markersize=3, linewidth=2, label=label, color=colors[label],
        )
    axes[0].set_title("Cumulative prompt content")
    axes[0].set_xlabel("Synthetic model request")
    axes[0].set_ylabel("Thousand serialized characters")
    axes[0].grid(alpha=0.25)
    axes[0].legend()
    axes[1].bar(
        list(scenarios),
        [data["requests"] for data in scenarios.values()],
        color=[colors[label] for label in scenarios],
    )
    axes[1].set_title("Requests before stopping")
    axes[1].set_ylabel("Synthetic model requests")
    axes[1].set_ylim(0, 27)
    for index, data in enumerate(scenarios.values()):
        axes[1].text(index, data["requests"] + 0.5, str(data["requests"]), ha="center")
    figure.suptitle("Identical tool-error loop, offline replay", fontsize=13)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, bbox_inches="tight")
    svg = args.output.read_text(encoding="utf-8")
    args.output.write_text(re.sub(r"[ \t]+(?=\r?$)", "", svg, flags=re.MULTILINE), encoding="utf-8")
    figure.savefig(args.output.with_suffix(".png"), dpi=180, bbox_inches="tight")


if __name__ == "__main__":
    main()
