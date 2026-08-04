#!/usr/bin/env python3
"""Render dependency-free SVG charts from matched-eval summaries."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


WIDTH = 1120
HEIGHT = 620
QWEN = "#0F766E"
FRONTIER = "#C2410C"
INK = "#172554"
MUTED = "#64748B"
GRID = "#E2E8F0"
PAPER = "#FFFFFF"


def esc(value: object) -> str:
    return html.escape(str(value))


def svg_start(title: str, subtitle: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="20" fill="{PAPER}"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}</style>',
        f'<text x="56" y="58" font-size="28" font-weight="700" fill="{INK}">{esc(title)}</text>',
        f'<text x="56" y="86" font-size="15" fill="{MUTED}">{esc(subtitle)}</text>',
    ]


def legend(lines: list[str], y: int = 112) -> None:
    lines.extend([
        f'<rect x="56" y="{y}" width="14" height="14" rx="3" fill="{QWEN}"/>',
        f'<text x="78" y="{y + 12}" font-size="14" fill="{INK}">Qwen3.6-27B</text>',
        f'<rect x="205" y="{y}" width="14" height="14" rx="3" fill="{FRONTIER}"/>',
        f'<text x="227" y="{y + 12}" font-size="14" fill="{INK}">Claude Opus 5</text>',
    ])


def finish(lines: list[str], output: Path) -> None:
    lines.append('</svg>')
    output.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def score_chart(summaries: list[dict[str, Any]], output: Path) -> None:
    lines = svg_start(
        "Matched OSWorld score by evaluation set",
        "Same tasks, same frozen harness, same 40-call budget; higher is better.",
    )
    legend(lines)
    left, top, plot_w, plot_h = 90, 165, 970, 355
    for tick in range(0, 101, 20):
        y = top + plot_h - plot_h * tick / 100
        lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="{GRID}"/>')
        lines.append(f'<text x="{left - 15}" y="{y + 5:.1f}" text-anchor="end" font-size="13" fill="{MUTED}">{tick}%</text>')
    group_w = plot_w / max(1, len(summaries))
    bar_w = min(76, group_w * 0.24)
    for index, summary in enumerate(summaries):
        center = left + group_w * (index + 0.5)
        for offset, arm, color in ((-bar_w * .55, 'qwen', QWEN), (bar_w * .55, 'frontier', FRONTIER)):
            value = float(summary[arm]['score_percent'])
            height = plot_h * value / 100
            x = center + offset - bar_w / 2
            y = top + plot_h - height
            lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{height:.1f}" rx="7" fill="{color}"/>')
            lines.append(f'<text x="{x + bar_w/2:.1f}" y="{y - 9:.1f}" text-anchor="middle" font-size="14" font-weight="700" fill="{color}">{value:.1f}%</text>')
        lines.append(f'<text x="{center:.1f}" y="{top + plot_h + 32}" text-anchor="middle" font-size="14" font-weight="600" fill="{INK}">{esc(summary["name"])}</text>')
        lines.append(f'<text x="{center:.1f}" y="{top + plot_h + 52}" text-anchor="middle" font-size="12" fill="{MUTED}">n={summary["pool_count"]}</text>')
    lines.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="{INK}" stroke-width="1.5"/>')
    finish(lines, output)


def efficiency_chart(summaries: list[dict[str, Any]], output: Path) -> None:
    lines = svg_start(
        "Efficiency: actions and dollars",
        "Mean model tool calls and provider cost per scored task; lower is better.",
    )
    legend(lines)
    panel_specs = [
        ("Mean tool calls", lambda s, a: float(s[a]['tool_calls_mean']), 40.0, "calls"),
        ("Provider cost per task", lambda s, a: float(s[a]['cost_usd']) / s['pool_count'], None, "USD"),
    ]
    for panel, (label, getter, fixed_max, unit) in enumerate(panel_specs):
        x0 = 55 + panel * 540
        top, plot_w, plot_h = 175, 475, 325
        values = [getter(summary, arm) for summary in summaries for arm in ('qwen', 'frontier')]
        ceiling = fixed_max or max(values + [0.01]) * 1.15
        lines.append(f'<text x="{x0}" y="150" font-size="17" font-weight="700" fill="{INK}">{esc(label)}</text>')
        group_w = plot_w / max(1, len(summaries))
        bar_w = min(54, group_w * .23)
        for index, summary in enumerate(summaries):
            center = x0 + group_w * (index + .5)
            for offset, arm, color in ((-bar_w*.55, 'qwen', QWEN), (bar_w*.55, 'frontier', FRONTIER)):
                value = getter(summary, arm)
                height = plot_h * value / ceiling
                x = center + offset - bar_w/2
                y = top + plot_h - height
                lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{height:.1f}" rx="6" fill="{color}"/>')
                display = f'${value:.2f}' if unit == 'USD' else f'{value:.1f}'
                lines.append(f'<text x="{x + bar_w/2:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="12" font-weight="700" fill="{color}">{display}</text>')
            lines.append(f'<text x="{center:.1f}" y="{top + plot_h + 28}" text-anchor="middle" font-size="12" fill="{INK}">{esc(summary["name"])}</text>')
        lines.append(f'<line x1="{x0}" y1="{top + plot_h}" x2="{x0 + plot_w}" y2="{top + plot_h}" stroke="{INK}"/>')
    finish(lines, output)


def gap_chart(summaries: list[dict[str, Any]], output: Path) -> None:
    lines = svg_start(
        "Qwen minus frontier: paired score gap",
        "Point estimate with task-resampled 95% interval. The shaded band is the ±2-point target.",
    )
    left, top, plot_w, row_h = 210, 155, 840, 90
    low = min([-50.0] + [float(s['paired_bootstrap_gap_95'][0]) for s in summaries])
    high = max([25.0] + [float(s['paired_bootstrap_gap_95'][1]) for s in summaries])
    low = math_floor_10(low)
    high = math_ceil_10(high)
    scale = lambda value: left + (value - low) / (high - low) * plot_w
    band_x = scale(-2)
    lines.append(f'<rect x="{band_x:.1f}" y="{top - 30}" width="{scale(2)-band_x:.1f}" height="{row_h*len(summaries)}" fill="#CCFBF1"/>')
    for tick in range(int(low), int(high) + 1, 10):
        x = scale(tick)
        lines.append(f'<line x1="{x:.1f}" y1="{top - 30}" x2="{x:.1f}" y2="{top + row_h*len(summaries)-30}" stroke="{GRID}"/>')
        lines.append(f'<text x="{x:.1f}" y="{top + row_h*len(summaries)}" text-anchor="middle" font-size="12" fill="{MUTED}">{tick:+d}</text>')
    zero = scale(0)
    lines.append(f'<line x1="{zero:.1f}" y1="{top - 30}" x2="{zero:.1f}" y2="{top + row_h*len(summaries)-30}" stroke="{INK}" stroke-width="2"/>')
    for index, summary in enumerate(summaries):
        y = top + index * row_h
        point = float(summary['qwen_minus_frontier_points'])
        ci_low, ci_high = map(float, summary['paired_bootstrap_gap_95'])
        lines.append(f'<text x="{left - 18}" y="{y + 5}" text-anchor="end" font-size="14" font-weight="600" fill="{INK}">{esc(summary["name"])}</text>')
        lines.append(f'<line x1="{scale(ci_low):.1f}" y1="{y}" x2="{scale(ci_high):.1f}" y2="{y}" stroke="{QWEN}" stroke-width="5" stroke-linecap="round"/>')
        lines.append(f'<circle cx="{scale(point):.1f}" cy="{y}" r="10" fill="{QWEN}" stroke="white" stroke-width="3"/>')
        lines.append(f'<text x="{scale(point):.1f}" y="{y - 18}" text-anchor="middle" font-size="13" font-weight="700" fill="{QWEN}">{point:+.1f} pt</text>')
    lines.append(f'<text x="{(scale(-2)+scale(2))/2:.1f}" y="{top - 40}" text-anchor="middle" font-size="12" font-weight="700" fill="{QWEN}">TARGET</text>')
    finish(lines, output)


def math_floor_10(value: float) -> float:
    return float(int(value // 10) * 10)


def math_ceil_10(value: float) -> float:
    return float(int(-(-value // 10)) * 10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("summaries", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summaries = [json.loads(path.read_text(encoding='utf-8')) for path in args.summaries]
    if not all(summary.get('valid') for summary in summaries):
        raise SystemExit('refusing to chart an invalid matched run')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    score_chart(summaries, args.output_dir / 'score-by-set.svg')
    efficiency_chart(summaries, args.output_dir / 'efficiency-by-set.svg')
    gap_chart(summaries, args.output_dir / 'paired-gap.svg')
    print(json.dumps({
        'charts': [str(args.output_dir / name) for name in (
            'score-by-set.svg', 'efficiency-by-set.svg', 'paired-gap.svg'
        )]
    }, indent=2))


if __name__ == '__main__':
    main()
