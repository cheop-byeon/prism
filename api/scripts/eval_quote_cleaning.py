"""Evaluate email quote cleaning against real mbox exports.

Samples messages from local IETF mailarchive mbox files (the ones written by
the "Download mbox" / preprocess flow under storage/ietf-downloads), runs each
body through the naive and advanced cleaning modes, and produces:

- a summary table on stdout (size reduction, unchanged count, safety-valve
  fallbacks per mode)
- a markdown report with the full ORIGINAL / NAIVE / ADVANCED text of every
  sampled message, for side-by-side eyeballing

Usage (from the api/ directory):

    uv run python scripts/eval_quote_cleaning.py <mbox-file-or-dir> \
        [--sample 50] [--seed 42] [--max-chars 2500] [--report PATH]

When given a directory, all *.mbox files are used except "*-dup.mbox"
duplicates. The report defaults to quote-cleaning-eval.md next to the first
input file.
"""

# ruff: noqa: T201, S311 — stdout output and non-crypto sampling are the point of a CLI eval tool

from __future__ import annotations

import argparse
import email as email_lib
import email.header as email_header_lib
import random
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script from the api/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.email_quote_cleaning import (
    _MIN_CLEANED_CHARS,  # eval-only: used to attribute unchanged output to the safety valve
    _QUOTE_LINE_RE,
    _clean_advanced,
    clean_email_body,
)

_MODES = ("naive", "advanced")


@dataclass
class MessageSample:
    source: str
    subject: str
    body: str


@dataclass
class ModeResult:
    cleaned: str
    reduction_pct: float
    unchanged: bool
    valve_fallback: bool


def _decode_header(value: str) -> str:
    try:
        parts = email_header_lib.decode_header(value)
        decoded = ""
        for text, charset in parts:
            if isinstance(text, bytes):
                decoded += text.decode(charset or "utf-8", errors="replace")
            else:
                decoded += text
        return decoded
    except Exception:
        return value


def _extract_text_body(msg: email_lib.message.Message) -> str | None:
    parts: list[str] = []
    candidates = msg.walk() if msg.is_multipart() else [msg]
    for part in candidates:
        if part.get_content_type() != "text/plain":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                parts.append(payload.decode("utf-8", errors="replace"))
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    return text or None


def _parse_mbox(path: Path) -> list[MessageSample]:
    content = path.read_bytes()
    raw_parts = re.split(rb"(?m)^From ", content)
    samples: list[MessageSample] = []
    for raw in raw_parts:
        if not raw or raw.startswith(b"# "):
            continue
        try:
            msg = email_lib.message_from_bytes(b"From " + raw)
        except Exception:
            continue
        body = _extract_text_body(msg)
        if not body or not body.strip():
            continue
        samples.append(
            MessageSample(
                source=path.name,
                subject=_decode_header(msg.get("Subject", "")).strip() or "(no subject)",
                body=body.strip(),
            )
        )
    return samples


def _raw_cleaned_length(body: str, mode: str) -> int:
    """Length of the cleaned body *before* the safety valve is applied."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if mode == "naive":
        kept = [line for line in lines if not _QUOTE_LINE_RE.match(line)]
        return len(re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip())
    return len(_clean_advanced(lines).strip())


def _evaluate(sample: MessageSample, mode: str) -> ModeResult:
    cleaned = clean_email_body(sample.body, mode=mode)  # type: ignore[arg-type]
    original = sample.body.strip()
    unchanged = cleaned == original
    valve_fallback = unchanged and _raw_cleaned_length(sample.body, mode) < _MIN_CLEANED_CHARS
    reduction = 100.0 * (1.0 - len(cleaned) / len(original)) if original else 0.0
    return ModeResult(
        cleaned=cleaned,
        reduction_pct=reduction,
        unchanged=unchanged and not valve_fallback,
        valve_fallback=valve_fallback,
    )


def _collect_mbox_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.glob("*.mbox") if not p.name.endswith("-dup.mbox"))
    raise FileNotFoundError(f"No such file or directory: {target}")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n… [truncated, {len(text) - max_chars} more chars]"


def _write_report(
    path: Path, samples: list[MessageSample], results: dict[int, dict[str, ModeResult]], max_chars: int
) -> None:
    lines: list[str] = ["# Quote cleaning evaluation", ""]
    for index, sample in enumerate(samples):
        lines.append(f"## {index + 1}. {sample.subject}")
        lines.append(f"*source: {sample.source}, original {len(sample.body)} chars*")
        lines.append("")
        lines.append("### original")
        lines.append("```text")
        lines.append(_truncate(sample.body, max_chars))
        lines.append("```")
        for mode in _MODES:
            result = results[index][mode]
            if result.valve_fallback:
                status = f"safety-valve fallback (cleaned under {_MIN_CLEANED_CHARS} chars, original kept)"
            elif result.unchanged:
                status = "unchanged"
            else:
                status = f"-{result.reduction_pct:.0f}% ({len(result.cleaned)} chars)"
            lines.append(f"### {mode} — {status}")
            lines.append("```text")
            lines.append(_truncate(result.cleaned, max_chars))
            lines.append("```")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="mbox file or directory containing *.mbox files")
    parser.add_argument("--sample", type=int, default=50, help="number of messages to sample (default: 50)")
    parser.add_argument("--seed", type=int, default=42, help="sampling seed for reproducibility (default: 42)")
    parser.add_argument("--max-chars", type=int, default=2500, help="truncate bodies in the report (default: 2500)")
    parser.add_argument(
        "--report", type=Path, default=None, help="report path (default: quote-cleaning-eval.md next to input)"
    )
    args = parser.parse_args()

    mbox_files = _collect_mbox_files(args.target)
    if not mbox_files:
        sys.exit(f"No .mbox files found under {args.target}")

    all_samples: list[MessageSample] = []
    for mbox_file in mbox_files:
        all_samples.extend(_parse_mbox(mbox_file))
    if not all_samples:
        sys.exit("No parseable messages with text bodies found.")

    rng = random.Random(args.seed)
    samples = rng.sample(all_samples, min(args.sample, len(all_samples)))

    results: dict[int, dict[str, ModeResult]] = {}
    for index, sample in enumerate(samples):
        results[index] = {mode: _evaluate(sample, mode) for mode in _MODES}

    print(
        f"Parsed {len(all_samples)} messages from {len(mbox_files)} file(s); "
        f"sampled {len(samples)} (seed {args.seed})."
    )
    print()
    header = f"{'mode':<10} {'mean redux':>10} {'median':>8} {'max':>6} {'cleaned':>8} {'unchanged':>10} {'valve':>6}"
    print(header)
    print("-" * len(header))
    for mode in _MODES:
        mode_results = [results[i][mode] for i in range(len(samples))]
        reductions = [r.reduction_pct for r in mode_results if not r.unchanged and not r.valve_fallback]
        cleaned_count = len(reductions)
        unchanged_count = sum(1 for r in mode_results if r.unchanged)
        valve_count = sum(1 for r in mode_results if r.valve_fallback)
        mean_reduction = statistics.mean(reductions) if reductions else 0.0
        median_reduction = statistics.median(reductions) if reductions else 0.0
        max_reduction = max(reductions) if reductions else 0.0
        print(
            f"{mode:<10} {mean_reduction:>9.1f}% {median_reduction:>7.1f}% {max_reduction:>5.0f}%"
            f" {cleaned_count:>8} {unchanged_count:>10} {valve_count:>6}"
        )

    total_before = sum(len(s.body) for s in samples)
    for mode in _MODES:
        total_after = sum(len(results[i][mode].cleaned) for i in range(len(samples)))
        smaller_pct = 100.0 * (1 - total_after / total_before)
        print(f"\ntotal chars {mode:<9} {total_before:,} -> {total_after:,} ({smaller_pct:.1f}% smaller)")

    report_path = args.report or (mbox_files[0].parent / "quote-cleaning-eval.md")
    _write_report(report_path, samples, results, args.max_chars)
    print(f"\nSide-by-side report: {report_path}")


if __name__ == "__main__":
    main()
