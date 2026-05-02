#!/usr/bin/env python3
"""
Summarize mirror changes with Gemini and send the digest with Resend.

The digest intentionally includes only added and modified Markdown files. Deleted
files are omitted to avoid sending huge historical content in the prompt/email.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Sequence
from urllib.request import Request, urlopen

DEFAULT_TO = "suenaga.kosuke@classmethod.jp"
DEFAULT_MODEL = "gemini-3-flash-preview"
MAX_DIFF_CHARS_PER_FILE = 12_000
MAX_TOTAL_DIFF_CHARS = 90_000
MIRROR_DIRS = ("blog", "docs", "code-docs")


@dataclass(frozen=True)
class ChangedFile:
    status: str
    path: str


def run_git(args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def discover_changed_markdown(base: str, head: str) -> List[ChangedFile]:
    output = run_git(
        [
            "diff",
            "--name-status",
            "--diff-filter=AM",
            base,
            head,
            "--",
            *MIRROR_DIRS,
        ]
    )
    files: List[ChangedFile] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1]
        if path.endswith(".md"):
            files.append(ChangedFile(status=status, path=path))
    return files


def build_diff_payload(base: str, head: str, files: Sequence[ChangedFile]) -> tuple[str, bool]:
    if not files:
        return "", False

    paths = [changed.path for changed in files]
    stat = run_git(["diff", "--stat", base, head, "--", *paths]).strip()
    sections = [f"Diff stat:\n{stat}\n"]
    truncated = False
    total_chars = sum(len(section) for section in sections)

    for changed in files:
        diff = run_git(
            [
                "diff",
                "--unified=3",
                base,
                head,
                "--",
                changed.path,
            ]
        )
        if len(diff) > MAX_DIFF_CHARS_PER_FILE:
            diff = diff[:MAX_DIFF_CHARS_PER_FILE] + "\n... [truncated: file diff too large]\n"
            truncated = True

        next_section = f"\n\n---\n{changed.status}\t{changed.path}\n\n{diff}"
        if total_chars + len(next_section) > MAX_TOTAL_DIFF_CHARS:
            sections.append("\n\n... [truncated: total diff budget reached]\n")
            truncated = True
            break

        sections.append(next_section)
        total_chars += len(next_section)

    return "".join(sections), truncated


def build_prompt(files: Sequence[ChangedFile], diff_payload: str, truncated: bool) -> str:
    file_list = "\n".join(f"- {changed.status}\t{changed.path}" for changed in files)
    truncation_note = (
        "Some diffs were truncated. Mention that the summary is based on a truncated diff."
        if truncated
        else "No diffs were truncated."
    )

    return f"""あなたは、Claude 関連ドキュメント mirror の更新内容を日本語のエンジニア向けに要約する担当です。

簡潔な日本語のメール本文を書いてください。機械的なフォーマット差分ではなく、意味のある内容変更に注目してください。

メール本文は必ず次の3セクション構成にしてください:

## 変更点の概要
追加・変更された内容を、重要度の高いものから短く整理してください。

## どんな示唆があるか
今回の変更から読み取れるプロダクト、API、ドキュメント、開発者体験上の示唆を書いてください。

## 技術記事を書く場合どんなネタで書けそうか
技術記事として展開できそうなテーマ案を箇条書きで挙げてください。単なるタイトル案だけでなく、読者にとって何が学びになるかも短く添えてください。

ルール:
- 追加または変更された Markdown ファイルだけを要約してください。
- 削除されたファイルには触れないでください。
- 必要に応じて `blog`、`docs`、`code-docs` の mirror ごとに整理してください。
- 読みやすく、短時間で把握できる構成にしてください。
- 最後に短い「Changed files」セクションを補足として入れ、対象ファイルのパスを列挙してください。
- 差分の大半がメタデータだけ、または意味のある本文変更がない場合は、その旨を明確に書いてください。
- {truncation_note}

Changed files:
{file_list}

Diff:
{diff_payload}
"""


def summarize_with_gemini(prompt: str) -> str:
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is required")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty summary")
        return text
    finally:
        client.close()


def send_with_resend(subject: str, body: str, to_email: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is required")
    if not from_email:
        raise RuntimeError("RESEND_FROM is required")

    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": body,
        "html": render_html_email(body),
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Resend returned HTTP {response.status}")


def render_html_email(body: str) -> str:
    escaped = html.escape(body)
    return f"""<!doctype html>
<html>
  <body>
    <pre style="font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; line-height: 1.5;">{escaped}</pre>
  </body>
</html>
"""


def build_subject(files: Sequence[ChangedFile]) -> str:
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    added = sum(1 for changed in files if changed.status == "A")
    modified = sum(1 for changed in files if changed.status == "M")
    return f"Claude mirrors updated - {today} ({added} added, {modified} modified)"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an AI-generated update digest email.")
    parser.add_argument("--base", default="HEAD~1", help="Base git revision for diff.")
    parser.add_argument("--head", default="HEAD", help="Head git revision for diff.")
    parser.add_argument(
        "--to",
        default=os.environ.get("MAIL_TO", DEFAULT_TO),
        help="Recipient email address. Defaults to MAIL_TO or the project default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changed files and prompt size without calling Gemini or Resend.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    files = discover_changed_markdown(args.base, args.head)
    if not files:
        print("No added or modified Markdown files found; skipping digest.")
        return 0

    diff_payload, truncated = build_diff_payload(args.base, args.head, files)
    prompt = build_prompt(files, diff_payload, truncated)
    subject = build_subject(files)

    if args.dry_run:
        print(subject)
        print(f"changed_markdown_files={len(files)}")
        print(f"prompt_chars={len(prompt)}")
        print("changed_files:")
        for changed in files:
            print(f"{changed.status}\t{changed.path}")
        return 0

    summary = summarize_with_gemini(prompt)
    send_with_resend(subject, summary, args.to)
    print(f"Sent digest to {args.to}: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
