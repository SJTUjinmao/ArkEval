#!/bin/bash
cd $ROOT

if [ ! -f .gitignore ]; then
    touch .gitignore
    echo "Created new .gitignore file"
fi

declare -a ignores=(
    # ts
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
)

added=0
existing=0

for ignore in "${ignores[@]}"
do
    if ! grep -Fxq "$ignore" .gitignore; then
        echo "$ignore" >> .gitignore
        ((added++))
    else
        ((existing++))
    fi
done

echo "Added $added new entries to .gitignore"
echo "Found $existing existing entries"
echo "Done! .gitignore has been updated."