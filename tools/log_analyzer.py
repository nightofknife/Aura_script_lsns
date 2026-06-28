from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<level>[A-Z]+)\s* - "
    r"\[cid:(?P<cid>[^\]]*)\] - "
    r"(?P<logger>.*?) - "
    r"(?P<callsite>.*?) - "
    r"(?P<message>.*)$"
)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
LIKELY_ISSUE_TERMS = (
    "error",
    "exception",
    "traceback",
    "failed",
    "failure",
    "timeout",
    "timed out",
    "cannot",
    "unable",
)


@dataclass(frozen=True)
class ParsedLogEntry:
    timestamp: datetime
    level: str
    cid: str
    logger_name: str
    callsite: str
    message: str
    line_number: int
    raw_lines: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(timespec="milliseconds"),
            "level": self.level,
            "cid": self.cid,
            "logger_name": self.logger_name,
            "callsite": self.callsite,
            "message": self.message,
            "line_number": self.line_number,
            "raw_lines": list(self.raw_lines),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze Aura plain-text session logs and surface likely issues."
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Explicit log file path. If omitted, the latest session log under logs/ is used.",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=REPO_ROOT / "logs",
        help="Directory used when resolving the latest log. Defaults to <repo>/logs.",
    )
    parser.add_argument(
        "--level",
        action="append",
        default=[],
        help="Filter by log level. Repeat the flag to include multiple levels.",
    )
    parser.add_argument("--cid", help="Filter by correlation id.")
    parser.add_argument("--keyword", help="Case-insensitive keyword filter on log message text.")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of matched entries shown in text or JSON output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable text.",
    )
    return parser


def resolve_log_file(explicit_file: Path | None, logs_dir: Path) -> Path:
    if explicit_file is not None:
        file_path = explicit_file.expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Log file not found: {file_path}")
        return file_path

    log_dir = logs_dir.expanduser().resolve()
    if not log_dir.is_dir():
        raise FileNotFoundError(f"Log directory not found: {log_dir}")

    candidates = sorted(
        log_dir.glob("aura_session_*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No session logs found under: {log_dir}")
    return candidates[0]


def parse_log_file(file_path: Path) -> tuple[list[ParsedLogEntry], int]:
    entries: list[ParsedLogEntry] = []
    unparsable_lines = 0

    current_match: re.Match[str] | None = None
    current_lines: list[str] = []
    current_line_number = 0

    def flush_current() -> None:
        nonlocal current_match, current_lines, current_line_number
        if current_match is None:
            return
        timestamp = datetime.strptime(current_match.group("timestamp"), TIMESTAMP_FORMAT)
        message_lines = [current_match.group("message")]
        if current_lines:
            message_lines.extend(current_lines[1:])
        entries.append(
            ParsedLogEntry(
                timestamp=timestamp,
                level=current_match.group("level").strip(),
                cid=current_match.group("cid").strip() or "-",
                logger_name=current_match.group("logger").strip(),
                callsite=current_match.group("callsite").strip(),
                message="\n".join(part.rstrip("\r\n") for part in message_lines).strip(),
                line_number=current_line_number,
                raw_lines=tuple(current_lines),
            )
        )
        current_match = None
        current_lines = []
        current_line_number = 0

    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        for index, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            match = LOG_LINE_RE.match(line)
            if match:
                flush_current()
                current_match = match
                current_lines = [line]
                current_line_number = index
                continue

            if current_match is None:
                if line.strip():
                    unparsable_lines += 1
                continue

            current_lines.append(line)

    flush_current()
    return entries, unparsable_lines


def normalize_levels(level_values: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for value in level_values:
        for item in str(value).split(","):
            item = item.strip().upper()
            if item:
                normalized.add(item)
    return normalized


def filter_entries(
    entries: Sequence[ParsedLogEntry],
    *,
    levels: set[str] | None = None,
    cid: str | None = None,
    keyword: str | None = None,
) -> list[ParsedLogEntry]:
    keyword_lower = keyword.lower() if keyword else None
    target_cid = cid.strip() if cid else None

    filtered: list[ParsedLogEntry] = []
    for entry in entries:
        if levels and entry.level.upper() not in levels:
            continue
        if target_cid and entry.cid != target_cid:
            continue
        if keyword_lower and keyword_lower not in entry.message.lower():
            continue
        filtered.append(entry)
    return filtered


def is_likely_issue(entry: ParsedLogEntry) -> bool:
    if entry.level.upper() in {"WARNING", "ERROR", "CRITICAL"}:
        return True
    message_lower = entry.message.lower()
    return any(term in message_lower for term in LIKELY_ISSUE_TERMS)


def summarize_entries(
    file_path: Path,
    entries: Sequence[ParsedLogEntry],
    matched_entries: Sequence[ParsedLogEntry],
    *,
    unparsable_lines: int,
    limit: int,
) -> dict[str, object]:
    counts_by_level = Counter(entry.level for entry in entries)
    top_messages = Counter(entry.message.splitlines()[0] for entry in matched_entries if entry.message).most_common(10)
    likely_issues = [entry for entry in matched_entries if is_likely_issue(entry)]
    unique_cids = sorted({entry.cid for entry in entries if entry.cid and entry.cid != "-"})

    return {
        "file": str(file_path),
        "total_entries": len(entries),
        "matched_entries": len(matched_entries),
        "unparsable_lines": unparsable_lines,
        "counts_by_level": dict(sorted(counts_by_level.items())),
        "first_timestamp": entries[0].timestamp.isoformat(timespec="milliseconds") if entries else None,
        "last_timestamp": entries[-1].timestamp.isoformat(timespec="milliseconds") if entries else None,
        "unique_cids": unique_cids,
        "top_messages": [{"count": count, "message": message} for message, count in top_messages],
        "likely_issue_count": len(likely_issues),
        "likely_issues": [entry.to_dict() for entry in likely_issues[:limit]],
        "matches": [entry.to_dict() for entry in matched_entries[:limit]],
    }


def format_summary_text(summary: dict[str, object], *, levels: set[str], cid: str | None, keyword: str | None) -> str:
    counts_by_level = summary["counts_by_level"]
    counts_text = ", ".join(
        f"{level}={count}" for level, count in counts_by_level.items()
    ) if counts_by_level else "none"
    filters: list[str] = []
    if levels:
        filters.append(f"levels={','.join(sorted(levels))}")
    if cid:
        filters.append(f"cid={cid}")
    if keyword:
        filters.append(f"keyword={keyword}")

    lines = [
        f"Log file: {summary['file']}",
        f"Time range: {summary['first_timestamp']} -> {summary['last_timestamp']}",
        f"Entries: {summary['total_entries']} parsed, {summary['unparsable_lines']} unparsable lines",
        f"Counts by level: {counts_text}",
        f"Matched entries: {summary['matched_entries']}"
        + (f" ({'; '.join(filters)})" if filters else ""),
        f"Unique cids: {len(summary['unique_cids'])}",
        f"Likely issues: {summary['likely_issue_count']}",
    ]

    top_messages = summary["top_messages"]
    if top_messages:
        lines.append("")
        lines.append("Top repeated messages:")
        for item in top_messages:
            lines.append(f"- {item['count']}x {item['message']}")

    likely_issues = summary["likely_issues"]
    if likely_issues:
        lines.append("")
        lines.append("Likely issue samples:")
        for item in likely_issues:
            message = str(item["message"]).splitlines()[0]
            lines.append(
                f"- L{item['line_number']} {item['timestamp']} {item['level']} cid={item['cid']} {message}"
            )

    matches = summary["matches"]
    if matches:
        lines.append("")
        lines.append("Matched entries:")
        for item in matches:
            message = str(item["message"]).splitlines()[0]
            lines.append(
                f"- L{item['line_number']} {item['timestamp']} {item['level']} cid={item['cid']} {message}"
            )

    return "\n".join(lines)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit <= 0:
        parser.error("--limit must be greater than 0")

    levels = normalize_levels(args.level)

    try:
        file_path = resolve_log_file(args.file, args.logs_dir)
        entries, unparsable_lines = parse_log_file(file_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    matched_entries = filter_entries(
        entries,
        levels=levels or None,
        cid=args.cid,
        keyword=args.keyword,
    )
    summary = summarize_entries(
        file_path,
        entries,
        matched_entries,
        unparsable_lines=unparsable_lines,
        limit=args.limit,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "filters": {
                        "levels": sorted(levels),
                        "cid": args.cid,
                        "keyword": args.keyword,
                        "limit": args.limit,
                    },
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_summary_text(summary, levels=levels, cid=args.cid, keyword=args.keyword))
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
