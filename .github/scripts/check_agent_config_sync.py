"""Fail CI when agent config hand-copied across tools drifts apart.

Claude Code reads .claude/, Codex reads .codex/ and .agents/. The same skill and
reviewer live in each, copied by hand -- and the Codex reviewer once pointed at a
portfolio-saas/AGENTS.md that never existed. Run from the repo root.
"""
import sys
import tomllib
from pathlib import Path

SKILL_PAIRS = [
    (".claude/skills/new-chart/SKILL.md", ".agents/skills/new-chart/SKILL.md"),
]
AGENT_PAIRS = [
    (".claude/agents/financial-invariant-reviewer.md", ".codex/agents/financial-invariant-reviewer.toml"),
]


def claude_agent_body(path):
    """Instructions of a Claude agent: everything after the closing frontmatter ---."""
    return Path(path).read_text().split("---", 2)[2]


def codex_agent_body(path):
    return tomllib.loads(Path(path).read_text())["developer_instructions"]


def in_sync(claude_body, codex_body):
    # Strict on purpose: any per-tool allowance is where the AGENTS.md drift hid.
    return claude_body.strip() == codex_body.strip()


def main():
    failures = []
    for a, b in SKILL_PAIRS:
        if Path(a).read_bytes() != Path(b).read_bytes():
            failures.append(f"{a} != {b}")
    for md, toml in AGENT_PAIRS:
        if not in_sync(claude_agent_body(md), codex_agent_body(toml)):
            failures.append(f"{md} and {toml} instructions differ")
    for f in failures:
        print(f"drift: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
