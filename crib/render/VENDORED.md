# Vendored: markdown renderer

These files are copied **verbatim** from `zsh-ai`
(`~/Development/zsh/zsh-ai/src/zsh_ai/render/`) so the `crib` CLI can render
note markdown through the same rich pipeline used there.

Kept byte-identical on purpose — the plan is to extract this into a shared
package later, so a clean move/diff stays possible. Don't edit in place; if a
fix is needed, make it upstream and re-copy.

Requires the `render` extra: `pip install 'cribsheet[render]'` (rich +
markdown-it-py). The CLI degrades to raw text when those aren't installed.
