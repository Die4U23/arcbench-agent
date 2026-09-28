"""Plot offline prompt-prefix probe output (optional dev dependency: matplotlib)."""

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
    parser.add_argument("data", type=Path, help="Combined probe JSON with named scenarios")
    parser.add_argument("--output", type=Path, required=True, help="Output SVG path")
    args = parser.parse_args()
    scenarios = json.loads(args.data.read_text(encoding="utf-8"))["scenarios"]
    colors = {"10:43 append-only": "#2563eb", "12:56 rolling": "#dc2626", "current bounded epochs": "#059669"}
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.4), constrained_layout=True)
    for name, data in scenarios.items():
        samples = data["samples"]
        x = [item["request"] for item in samples]
        color = colors.get(name)
        axes[0].plot(x, [item["cumulative_fresh_chars"] / 1000 for item in samples],
                     label=name, color=color, linewidth=2.2)
        axes[1].plot(x, [item["cumulative_prompt_chars"] / 1000 for item in samples],
                     label=name, color=color, linewidth=2.2)
    axes[0].set_title("Cumulative new prefix content")
    axes[1].set_title("Cumulative prompt content")
    for axis in axes:
        axis.set_xlabel("Synthetic model request")
        axis.set_ylabel("Thousand serialized characters")
        axis.grid(alpha=0.25)
        axis.set_xlim(left=1)
    axes[0].legend(loc="upper left", fontsize=8)
    figure.suptitle("ARC-Bench Agent context strategies (offline prefix proxy)", fontsize=13)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, bbox_inches="tight")
    svg = args.output.read_text(encoding="utf-8")
    args.output.write_text(re.sub(r"[ \t]+(?=\r?$)", "", svg, flags=re.MULTILINE), encoding="utf-8")
    figure.savefig(args.output.with_suffix(".png"), dpi=180, bbox_inches="tight")


if __name__ == "__main__":
    main()
