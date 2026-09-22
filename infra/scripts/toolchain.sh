#!/bin/sh
# Run a project tool with the toolchain on PATH, however this process was started.
#
#     infra/scripts/toolchain.sh uv run python infra/scripts/secret_scan.py
#
# Why this exists: the pre-commit hooks use `language: system` on purpose, so a hook and its
# CI gate run the same version of the same tool rather than a pre-commit-managed copy that can
# drift. The cost of that choice is that the tools have to be findable, and a `git commit` does
# not always inherit a login shell's PATH — an IDE's git integration, a GUI client or a cron
# job typically gets a much shorter one. A hook that works in the terminal and fails in the
# editor is worse than no hook, because it only fails for some of the people some of the time.
#
# So: prepend the standard install locations, then exec. Anything already on PATH still wins,
# because a developer who installed a tool elsewhere meant it.
set -eu

for dir in "$HOME/.local/bin" "$HOME/.foundry/bin" "$HOME/.cargo/bin" "$HOME/.bun/bin"; do
    case ":$PATH:" in
        *":$dir:"*) ;;
        *) [ -d "$dir" ] && PATH="$dir:$PATH" ;;
    esac
done
export PATH

if [ "$#" -eq 0 ]; then
    echo "usage: toolchain.sh <command> [args...]" >&2
    exit 2
fi

if ! command -v "$1" >/dev/null 2>&1; then
    tool="$1"
    echo "toolchain.sh: \`$tool\` not found, after adding the usual install locations to PATH." >&2
    echo >&2
    case "$tool" in
        uv)    echo "  Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
        forge) echo "  Install: curl -L https://foundry.paradigm.xyz | bash && foundryup -i v1.8.3" >&2 ;;
        pnpm)  echo "  Install: corepack enable --install-directory \"\$HOME/.local/bin\" pnpm" >&2 ;;
        *)     echo "  See the prerequisites in README.md." >&2 ;;
    esac
    echo >&2
    echo "  Then run: make setup" >&2
    exit 1
fi

exec "$@"
