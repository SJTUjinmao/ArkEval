# @yaml
# signature: search_dir <search_term> [<dir>]
# docstring: searches for search_term in all files in dir. If dir is not provided, searches in the current directory. Quote search terms containing shell metacharacters, for example search_dir 'build()' entry/src/main/ets
# arguments:
#   search_term:
#     type: string
#     description: the term to search for
#     required: true
#   dir:
#     type: string
#     description: the directory to search in (if not provided, searches in the current directory)
#     required: false
_mswe_normalize_search_path() {
    local path="$1"
    path="${path//\\//}"
    printf '%s' "$path" | tr '[:upper:]' '[:lower:]'
}

_mswe_search_target_is_blocked() {
    local normalized
    normalized="$(_mswe_normalize_search_path "$1")"
    case "/$normalized/" in
        *"/env_overlays/"*|*"/oh_modules/"*|*"/node_modules/"*|*"/.hvigor/"*|*"/build/"*|*"/.git/"*)
            return 0
            ;;
    esac
    case "$normalized" in
        *devecoapi*_sdk*|*"/deveco studio/"*|*"/command-line-tools/"*|*"/sdk/"*|*/sdk)
            return 0
            ;;
    esac
    return 1
}

_mswe_search_target_block_message() {
    local command_name="$1"
    local search_term="$2"
    local target="$3"
    echo "COMMAND_FORMAT_ERROR: command was not executed."
    echo "You wrote: $command_name \"$search_term\" \"$target\""
    echo "Problem: target is a large/generated/external directory (SDK, DevEco, env_overlays, oh_modules, node_modules, build, .hvigor, or .git)."
    echo "Correct syntax: $command_name <search_term> [<repo/project dir>]"
    echo "Example:"
    echo "$command_name 'distributedDeviceManager' entry/src/main/ets"
}

search_dir() {
    if command -v _mswe_late_no_diff_guard_enabled >/dev/null 2>&1 && _mswe_late_no_diff_guard_enabled; then
        echo "search_dir: blocked by late no-diff guard."
        echo "Reason: more than 20 agent steps have passed without any KNOWN DEFECT FILE diff."
        echo "Stop broad exploration. Open a KNOWN DEFECT FILE and use edit_file or str_replace for the smallest issue-related repair."
        return 2
    fi

    if [ $# -eq 1 ]; then
        local search_term="$1"
        local dir="./"
    elif [ $# -eq 2 ]; then
        local dir_arg="$2"
        if command -v _mswe_resolve_path >/dev/null 2>&1; then
            dir_arg="$(_mswe_resolve_path "$dir_arg")"
        fi
        if [ -d "$dir_arg" ]; then
            local search_term="$1"
            local dir="$dir_arg"
        else
            local reversed_dir="$1"
            if command -v _mswe_resolve_path >/dev/null 2>&1; then
                reversed_dir="$(_mswe_resolve_path "$reversed_dir")"
            fi
            if [ -d "$reversed_dir" ]; then
            local search_term="$2"
                local dir="$reversed_dir"
            echo "Note: interpreted reversed arguments as: search_dir \"$search_term\" \"$dir\""
        else
            echo "Directory $2 not found"
            return
        fi
        fi
    else
        echo "Usage: search_dir <search_term> [<dir>]"
        return
    fi
    dir=$(realpath "$dir")
    if _mswe_search_target_is_blocked "$dir"; then
        _mswe_search_target_block_message "search_dir" "$search_term" "$dir"
        return 2
    fi
    local matches=$(find "$dir" -type f ! -path '*/.*' -exec grep -nIH -- "$search_term" {} + | cut -d: -f1 | sort | uniq -c)
    # if no matches, return
    if [ -z "$matches" ]; then
        echo "No matches found for \"$search_term\" in $dir"
        return
    fi
    # Calculate total number of matches
    local num_matches=$(echo "$matches" | awk '{sum+=$1} END {print sum}')
    # calculate total number of files matched
    local num_files=$(echo "$matches" | wc -l | awk '{$1=$1; print $0}')
    # if num_files is > 100, print an error
    if [ $num_files -gt 100 ]; then
        echo "More than $num_files files matched for \"$search_term\" in $dir. Please narrow your search."
        return
    fi

    echo "Found $num_matches matches for \"$search_term\" in $dir:"
    echo "$matches" | awk '{$2=$2; gsub(/^\.+\/+/, "./", $2); print $2 " ("$1" matches)"}'
    echo "End of matches for \"$search_term\" in $dir"
}

# @yaml
# signature: search_file <search_term> [<file>]
# docstring: searches for search_term in file. If file is not provided, searches in the current open file. Quote search terms containing shell metacharacters, for example search_file 'build()' entry/src/main/ets/pages/Index.ets
# arguments:
#   search_term:
#     type: string
#     description: the term to search for
#     required: true
#   file:
#     type: string
#     description: the file to search in (if not provided, searches in the current open file)
#     required: false
search_file() {
    # Check if the first argument is provided
    if [ -z "$1" ]; then
        echo "Usage: search_file <search_term> [<file>]"
        return
    fi
    local search_term="$1"
    # Check if the second argument is provided
    if [ -n "$2" ]; then
        local target="$2"
        if command -v _mswe_resolve_path >/dev/null 2>&1; then
            target="$(_mswe_resolve_path "$target")"
        fi
        # Check if the provided argument is a valid file
        if [ -f "$target" ]; then
            if command -v _mswe_late_no_diff_guard_enabled >/dev/null 2>&1 && _mswe_late_no_diff_guard_enabled; then
                if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
                    if ! _mswe_guard_defect_file_edit "$target" "search_file" "read"; then
                        return 2
                    fi
                fi
            fi
            local file="$target"  # Set file if valid
        elif [ -d "$target" ]; then
            echo "Note: search_file target \"$2\" is a directory; searching recursively with search_dir instead."
            search_dir "$search_term" "$target"
            return
        else
            local reversed="$1"
            if command -v _mswe_resolve_path >/dev/null 2>&1; then
                reversed="$(_mswe_resolve_path "$reversed")"
            fi
            if [ -f "$reversed" ]; then
                if command -v _mswe_late_no_diff_guard_enabled >/dev/null 2>&1 && _mswe_late_no_diff_guard_enabled; then
                    if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
                        if ! _mswe_guard_defect_file_edit "$reversed" "search_file" "read"; then
                            return 2
                        fi
                    fi
                fi
                local file="$reversed"
            search_term="$2"
            echo "Note: interpreted reversed arguments as: search_file \"$search_term\" \"$file\""
            else
            echo "Usage: search_file <search_term> [<file>]"
            echo "Error: File name $2 not found. Please provide a valid file name."
            return  # Exit if the file is not valid
            fi
        fi
    else
        # Check if a file is open
        if [ -z "$CURRENT_FILE" ]; then
            echo "No file open. Use the open command first."
            return  # Exit if no file is open
        fi
        if command -v _mswe_late_no_diff_guard_enabled >/dev/null 2>&1 && _mswe_late_no_diff_guard_enabled; then
            if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
                if ! _mswe_guard_defect_file_edit "$CURRENT_FILE" "search_file" "read"; then
                    return 2
                fi
            fi
        fi
        local file="$CURRENT_FILE"  # Set file to the current open file
    fi
    file=$(realpath "$file")
    if _mswe_search_target_is_blocked "$file"; then
        _mswe_search_target_block_message "search_file" "$search_term" "$file"
        return 2
    fi
    # Use grep to directly get the desired formatted output
    local matches=$(grep -nH -- "$search_term" "$file")
    # Check if no matches were found
    if [ -z "$matches" ]; then
        echo "No matches found for \"$search_term\" in $file"
        return
    fi
    # Calculate total number of matches
    local num_matches=$(echo "$matches" | wc -l | awk '{$1=$1; print $0}')

    # calculate total number of lines matched
    local num_lines=$(echo "$matches" | cut -d: -f1 | sort | uniq | wc -l | awk '{$1=$1; print $0}')
    # if num_lines is > 100, print an error
    if [ $num_lines -gt 100 ]; then
        echo "More than $num_lines lines matched for \"$search_term\" in $file. Please narrow your search."
        return
    fi

    # Print the total number of matches and the matches themselves
    echo "Found $num_matches matches for \"$search_term\" in $file:"
    echo "$matches" | cut -d: -f1-2 | sort -u -t: -k2,2n | while IFS=: read -r filename line_number; do
        echo "Line $line_number:$(sed -n "${line_number}p" "$file")"
    done
    echo "End of matches for \"$search_term\" in $file"
}

# @yaml
# signature: find_file <file_name> [<dir>]
# docstring: finds all files with the given name in dir. If dir is not provided, searches in the current directory
# arguments:
#   file_name:
#     type: string
#     description: the name of the file to search for
#     required: true
#   dir:
#     type: string
#     description: the directory to search in (if not provided, searches in the current directory)
#     required: false
find_file() {
    if command -v _mswe_late_no_diff_guard_enabled >/dev/null 2>&1 && _mswe_late_no_diff_guard_enabled; then
        echo "find_file: blocked by late no-diff guard."
        echo "Reason: more than 20 agent steps have passed without any KNOWN DEFECT FILE diff."
        echo "Stop broad exploration. Open a KNOWN DEFECT FILE and use edit_file or str_replace for the smallest issue-related repair."
        return 2
    fi

    if [ $# -eq 1 ]; then
        local file_name="$1"
        local dir="./"
    elif [ $# -eq 2 ]; then
        local file_name="$1"
        local dir_arg="$2"
        if command -v _mswe_resolve_path >/dev/null 2>&1; then
            dir_arg="$(_mswe_resolve_path "$dir_arg")"
        fi
        if [ -d "$dir_arg" ]; then
            local dir="$dir_arg"
        else
            echo "Directory $2 not found"
            return
        fi
    else
        echo "Usage: find_file <file_name> [<dir>]"
        return
    fi

    dir=$(realpath "$dir")
    if _mswe_search_target_is_blocked "$dir"; then
        _mswe_search_target_block_message "find_file" "$file_name" "$dir"
        return 2
    fi
    local matches=$(find "$dir" -type f -name "$file_name")
    # if no matches, return
    if [ -z "$matches" ]; then
        echo "No matches found for \"$file_name\" in $dir"
        return
    fi
    # Calculate total number of matches
    local num_matches=$(echo "$matches" | wc -l | awk '{$1=$1; print $0}')
    echo "Found $num_matches matches for \"$file_name\" in $dir:"
    echo "$matches" | awk '{print $0}'
}
