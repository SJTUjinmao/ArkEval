#!/bin/bash
# Local-only ignores: use .git/info/exclude so we do NOT modify tracked .gitignore
# (which would pollute `git diff` / agent submissions with spurious .gitignore hunks).
cd "$ROOT"
mkdir -p .git/info
EXCLUDE_FILE=".git/info/exclude"

if ! grep -q "^# MSWE-agent arkts local excludes$" "$EXCLUDE_FILE" 2>/dev/null; then
    {
        echo ""
        echo "# MSWE-agent arkts local excludes"
    } >> "$EXCLUDE_FILE"
fi

declare -a ignores=(
    "node_modules/"
    "build/"
    "dist/"
    ".next/"
    "coverage/"
    ".env"
    "npm-debug.log*"
    "yarn-debug.log*"
    "yarn-error.log*"
    "*.js"
    "*.js.map"
    "*.d.ts"
    ".tsbuildinfo"
    ".hvigor/"
)

added=0
existing=0

for ignore in "${ignores[@]}"
do
    if ! grep -Fxq "$ignore" "$EXCLUDE_FILE" 2>/dev/null; then
        echo "$ignore" >> "$EXCLUDE_FILE"
        ((added++))
    else
        ((existing++))
    fi
done

echo "Added $added new entries to .git/info/exclude"
echo "Found $existing existing entries"
echo "Done! (tracked .gitignore left unchanged)"
