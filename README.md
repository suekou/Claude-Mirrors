# Claude Mirrors

Local Markdown mirrors for Claude-related public content.

## Mirrors

- `blog/`: Claude blog posts from <https://claude.com/blog>, grouped by publish year and month.
- `docs/`: Claude Developer Platform docs from <https://platform.claude.com/docs/en/>, using the URL path below `/docs/en/`.
- `code-docs/`: Claude Code docs from <https://code.claude.com/docs/en/overview>, using the URL path below `/docs/en/`.

## Fetch Locally

```bash
python3 scripts/fetch_claude_blog.py
python3 scripts/fetch_claude_docs.py
python3 scripts/fetch_claude_code_docs.py
```

Single-page examples:

```bash
python3 scripts/fetch_claude_blog.py --url https://claude.com/blog/building-ai-agents-for-the-enterprise
python3 scripts/fetch_claude_docs.py --url https://platform.claude.com/docs/en/intro
python3 scripts/fetch_claude_code_docs.py --url https://code.claude.com/docs/en/overview
```

## Storage Layout

```text
blog/
  2026/
    April/
      2026-04-30-building-ai-agents-for-the-enterprise.md
  blog_manifest.json

docs/
  intro.md
  build-with-claude/
    overview.md
  docs_manifest.json

code-docs/
  overview.md
  agent-sdk/
    overview.md
  code_docs_manifest.json
```

## GitHub Actions

`.github/workflows/update-mirrors.yml` updates all three mirrors once per day at 14:17 JST (`05:17 UTC`) and can also be run manually.

The schedule is intentionally after the observed upstream update windows:

- Claude Code docs sitemap showed recent updates around `03:31 UTC`, so `05:17 UTC` gives it roughly 1.5 hours of buffer.
- Claude blog's Webflow page showed `Last Published` around `20:33 UTC`, so the run happens well after that publishing window.
- Claude Developer Platform docs do not expose `lastmod` in the sitemap, so they are fetched in the same later window.

The workflow runs the mirrors sequentially, pauses 60 seconds between sources, and commits all changes together as `Update Claude mirrors`.
