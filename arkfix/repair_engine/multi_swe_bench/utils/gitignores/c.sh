#!/bin/bash
cd $ROOT

if [ ! -f .gitignore ]; then
    touch .gitignore
    echo "Created new .gitignore file"
fi

declare -a ignores=(
    # C/C++
    "build/"
    "Build/"
    "bin/"
    "lib/"
    "*.o"
    "*.out"
    "*.obj"
    "*.exe"
    "*.dll"
    "*.so"
    "*.dylib"
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