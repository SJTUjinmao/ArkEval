_mswe_resolve_path() {
    local input="$1"
    if [ -z "$input" ]; then
        return
    fi
    if [ -e "$input" ]; then
        printf '%s\n' "$input"
        return
    fi

    local normalized="${input#./}"
    local project_path="${MSWE_PROJECT_PATH#./}"
    if [ -n "$project_path" ] && [ "$project_path" != "." ]; then
        if [[ "$normalized" == "$project_path"/* ]]; then
            local stripped="${normalized#"$project_path"/}"
            if [ -e "$stripped" ]; then
                printf '%s\n' "$stripped"
                return
            fi
        fi
    fi

    if [ -n "$MSWE_NATIVE_REPO_ROOT" ] && [ -e "$MSWE_NATIVE_REPO_ROOT/$normalized" ]; then
        printf '%s\n' "$MSWE_NATIVE_REPO_ROOT/$normalized"
        return
    fi

    printf '%s\n' "$input"
}

_mswe_guard_defect_file_edit() {
    local target="$1"
    local action="${2:-edit}"
    local purpose="${3:-write}"
    local defect_json="${MSWE_DEFECT_FILES_JSON:-}"
    if [ -z "$defect_json" ] || [ "$defect_json" = "[]" ]; then
        return 0
    fi

    local python_cmd="${PYTHON:-python}"
    if ! command -v "$python_cmd" >/dev/null 2>&1; then
        if command -v python3 >/dev/null 2>&1; then
            python_cmd="python3"
        else
            echo "$action: cannot verify KNOWN DEFECT FILES scope because python is unavailable."
            echo "Operation blocked. Use only files listed in KNOWN DEFECT FILES."
            return 2
        fi
    fi

    "$python_cmd" - "$target" "$action" "$purpose" <<'PY'
import json
import os
import sys
from pathlib import Path

raw_target = sys.argv[1]
action = sys.argv[2]
purpose = sys.argv[3]

def normalize(value):
    value = str(value or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    parts = []
    for part in value.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)

try:
    defect_files = json.loads(os.environ.get("MSWE_DEFECT_FILES_JSON") or "[]")
except json.JSONDecodeError:
    defect_files = []

allowed = {normalize(item) for item in defect_files if str(item).strip()}
if not allowed:
    sys.exit(0)

cwd = Path.cwd().resolve()
target = Path(raw_target)
if not target.is_absolute():
    target = (cwd / target).resolve()
else:
    target = target.resolve()

native_root_raw = os.environ.get("MSWE_NATIVE_REPO_ROOT", "").strip()
project_raw = normalize(os.environ.get("MSWE_PROJECT_PATH", ""))
candidates = {normalize(raw_target), normalize(str(target))}

if native_root_raw:
    try:
        native_root = Path(native_root_raw).resolve()
        candidates.add(normalize(target.relative_to(native_root).as_posix()))
    except Exception:
        pass

try:
    cwd_rel = normalize(target.relative_to(cwd).as_posix())
    candidates.add(cwd_rel)
    if project_raw and project_raw != ".":
        candidates.add(normalize(project_raw + "/" + cwd_rel))
except Exception:
    pass

if project_raw and project_raw != ".":
    stripped_allowed = {
        normalize(path[len(project_raw) + 1 :])
        for path in allowed
        if path == project_raw or path.startswith(project_raw + "/")
    }
    allowed = allowed | stripped_allowed

if candidates & allowed:
    sys.exit(0)

if purpose == "read":
    print(f"{action}: blocked late read/search outside KNOWN DEFECT FILES.")
else:
    print(f"{action}: blocked write outside KNOWN DEFECT FILES.")
print(f"Target: {normalize(raw_target)}")
print("Allowed edit targets:")
for path in sorted(allowed):
    print(f"  - {path}")
if purpose == "read":
    print("Late no-diff guard is active: stop broad exploration and work inside the KNOWN DEFECT FILES.")
else:
    print("Use open/search for context files, but edit only the KNOWN DEFECT FILES listed above.")
sys.exit(2)
PY
}

_mswe_late_no_diff_guard_enabled() {
    [ "${MSWE_LATE_NO_DIFF_GUARD:-0}" = "1" ] || [ "${MSWE_LATE_NO_DIFF_GUARD:-}" = "true" ] || [ "${MSWE_LATE_NO_DIFF_GUARD:-}" = "TRUE" ]
}

_print() {
    local total_lines=$(awk 'END {print NR}' $CURRENT_FILE)
    echo "[File: $(realpath $CURRENT_FILE) ($total_lines lines total)]"
    
    local lines_above=$(jq -n "$CURRENT_LINE - $WINDOW/2" | jq '[0, .] | max | floor')
    local lines_below=$(jq -n "$total_lines - $CURRENT_LINE - $WINDOW/2" | jq '[0, .] | max | floor')
    
    if [ $lines_above -gt 0 ]; then
        echo "($lines_above more lines above)"
    fi
    
    # Calculate start line
    local start_line=$(jq -n "[$CURRENT_LINE - $WINDOW/2 + 1, 1] | max | floor")
    local end_line=$(jq -n "[$start_line + $WINDOW - 1, $total_lines] | min | floor")
    
    # Use awk instead of pipeline to avoid MSYS2 SIGPIPE crashes on large files
    awk -v s="$start_line" -v e="$end_line" 'NR>=s && NR<=e {print NR ":" $0} NR>e {exit}' "$CURRENT_FILE"
    
    if [ $lines_below -gt 0 ]; then
        echo "($lines_below more lines below)"
    fi
}

_rm_binaries(){
    for file in $(git status --porcelain | grep -E "^(M| M|\?\?|A| A)" | cut -c4-); do
        if [ -f "$file" ] && (file "$file" | grep -q "executable" || git check-attr binary "$file" | grep -q "binary: set"); then
            git rm -f "$file" 2>/dev/null || rm -f "$file"
            echo "Removed: $file"
        fi
    done
}

_constrain_line() {
    if [ -z "$CURRENT_FILE" ]
    then
        echo "No file open. Use the open command first."
        return
    fi
    local max_line=$(awk 'END {print NR}' $CURRENT_FILE)
    local half_window=$(jq -n "$WINDOW/2" | jq 'floor')
    export CURRENT_LINE=$(jq -n "[$CURRENT_LINE, $max_line - $half_window] | min")
    export CURRENT_LINE=$(jq -n "[$CURRENT_LINE, $half_window] | max")
}

# @yaml
# signature: open <path> [<line_number>]
# docstring: opens the file at the given path in the editor. If line_number is provided, the window will be move to include that line
# arguments:
#   path:
#     type: string
#     description: the path to the file to open
#     required: true
#   line_number:
#     type: integer
#     description: the line number to move the window to (if not provided, the window will start at the top of the file)
#     required: false
open() {
    if [ -z "$1" ]
    then
        echo "Usage: open <file>"
        return
    fi
    local target="$1"
    if command -v _mswe_resolve_path >/dev/null 2>&1; then
        target="$(_mswe_resolve_path "$target")"
    fi
    # Check if the second argument is provided
    if [ -n "$2" ]; then
        # Check if the provided argument is a valid number
        if ! [[ $2 =~ ^[0-9]+$ ]]; then
            echo "Usage: open <file> [<line_number>]"
            echo "Error: <line_number> must be a number"
            if [[ $1 =~ ^[0-9]+$ ]]; then
                echo "Hint: You likely reversed the arguments. Correct form is: open <file> $1"
                echo "Example: open path/to/File.ets $1"
            fi
            return  # Exit if the line number is not valid
        fi
        local max_line=$(awk 'END {print NR}' "$target")
        if [ $2 -gt $max_line ]; then
            echo "Warning: <line_number> ($2) is greater than the number of lines in the file ($max_line)"
            echo "Warning: Setting <line_number> to $max_line"
            local line_number=$(jq -n "$max_line")  # Set line number to max if greater than max
        elif [ $2 -lt 1 ]; then
            echo "Warning: <line_number> ($2) is less than 1"
            echo "Warning: Setting <line_number> to 1"
            local line_number=$(jq -n "1")  # Set line number to 1 if less than 1
        else
            local OFFSET=$(jq -n "$WINDOW/6" | jq 'floor')
            local line_number=$(jq -n "[$2 + $WINDOW/2 - $OFFSET, 1] | max | floor")
        fi
    else
        local line_number=$(jq -n "$WINDOW/2")  # Set default line number if not provided
    fi

    if [ -f "$target" ]; then
        if _mswe_late_no_diff_guard_enabled && command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
            if ! _mswe_guard_defect_file_edit "$target" "open" "read"; then
                return 2
            fi
        fi
        export CURRENT_FILE=$(realpath "$target")
        export CURRENT_LINE=$line_number
        _constrain_line
        _print
    elif [ -d "$target" ]; then
        echo "Error: $1 is a directory. You can only open files. Use cd or ls to navigate directories."
    else
        echo "File $1 not found"
    fi
}

# @yaml
# signature: goto <line_number>
# docstring: moves the window to show <line_number>
# arguments:
#   line_number:
#     type: integer
#     description: the line number to move the window to
#     required: true
goto() {
    if [ $# -gt 1 ]; then
        echo "goto allows only one line number at a time."
        return
    fi
    if [ -z "$CURRENT_FILE" ]
    then
        echo "No file open. Use the open command first."
        return
    fi
    if [ -z "$1" ]
    then
        echo "Usage: goto <line>"
        return
    fi
    if ! [[ $1 =~ ^[0-9]+$ ]]
    then
        echo "Usage: goto <line>"
        echo "Error: <line> must be a number"
        return
    fi
    local max_line=$(awk 'END {print NR}' $CURRENT_FILE)
    local target_line="$1"
    if [ $1 -gt $max_line ]
    then
        echo "Warning: <line> ($1) is greater than the number of lines in the file ($max_line)"
        echo "Warning: Setting <line> to $max_line"
        target_line="$max_line"
    fi
    local OFFSET=$(jq -n "$WINDOW/6" | jq 'floor')
    export CURRENT_LINE=$(jq -n "[$target_line + $WINDOW/2 - $OFFSET, 1] | max | floor")
    _constrain_line
    _print
}

# @yaml
# signature: scroll_down
# docstring: moves the window down {WINDOW} lines
scroll_down() {
    if [ -z "$CURRENT_FILE" ]
    then
        echo "No file open. Use the open command first."
        return
    fi
    export CURRENT_LINE=$(jq -n "$CURRENT_LINE + $WINDOW - $OVERLAP")
    _constrain_line
    _print
}

# @yaml
# signature: scroll_up
# docstring: moves the window up {WINDOW} lines
scroll_up() {
    if [ -z "$CURRENT_FILE" ]
    then
        echo "No file open. Use the open command first."
        return
    fi
    export CURRENT_LINE=$(jq -n "$CURRENT_LINE - $WINDOW + $OVERLAP")
    _constrain_line
    _print
}

# @yaml
# signature: create <filename>
# docstring: creates and opens a new file with the given name
# arguments:
#   filename:
#     type: string
#     description: the name of the file to create
#     required: true
create() {
    if [ -z "$1" ]; then
        echo "Usage: create <filename>"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
        if ! _mswe_guard_defect_file_edit "$1" "create"; then
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi

    # Check if the file already exists
    if [ -e "$1" ]; then
        echo "Error: File '$1' already exists."
        open "$1"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    # Create the file an empty new line
    if ! printf "\n" > "$1"; then
        echo "create: failed to create '$1'."
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    # Use the existing open command to open the created file
    open "$1"
    echo "EDIT_STATUS=APPLIED"
}

# @yaml
# signature: submit [ignored-path ...]
# docstring: auto-submits all eligible changed .ets/.ts files and terminates the session
submit() {
    cd $ROOT
    rm -f model.patch

    if [ $# -gt 0 ]; then
        echo "Note: submit path arguments are ignored; the system will auto-collect all eligible .ets/.ts diffs."
    fi

    _submit_is_blocked_artifact_path() {
        local path="$1"
        case "$path" in
            ""|/*|[A-Za-z]:*|../*|*/../*|:*|-*)
                echo "Error: invalid submit path '$path'. Use clean repo-relative file paths only."
                return 0
                ;;
            oh_modules|oh_modules/*|*/oh_modules|*/oh_modules/*|node_modules|node_modules/*|*/node_modules|*/node_modules/*|build|build/*|*/build|*/build/*|.hvigor|.hvigor/*|*/.hvigor|*/.hvigor/*|.cxx|.cxx/*|*/.cxx|*/.cxx/*|.preview|.preview/*|*/.preview|*/.preview/*|coverage|coverage/*|*/coverage|*/coverage/*|dist|dist/*|*/dist|*/dist/*|out|out/*|*/out|*/out/*)
                return 0
                ;;
        esac
        case "$path" in
            BuildProfile.ets|*/BuildProfile.ets|oh-package-lock.json5|*/oh-package-lock.json5|local.properties|*/local.properties|model.patch|*/model.patch|*.log|*.tmp|*.bak|*.orig|*.rej|*.patch|*.hap|*.har|*.hsp|*.app|*.apk|*.so|*.dll|*.zip|*.tar|*.gz)
                return 0
                ;;
        esac
        return 1
    }

    _submit_is_eligible_code_path() {
        case "$1" in
            *.ets|*.ts) return 0 ;;
            *) return 1 ;;
        esac
    }

    local defect_filter_file=""
    local previous_exit_trap=""
    _submit_cleanup_filter() {
        [ -z "${defect_filter_file:-}" ] && return
        rm -f -- "$defect_filter_file"
        defect_filter_file=""
        trap - EXIT
        if [ -n "$previous_exit_trap" ]; then
            eval "$previous_exit_trap"
        fi
        return 0
    }
    if [ -n "${MSWE_DEFECT_FILES_JSON:-}" ] && [ "${MSWE_DEFECT_FILES_JSON:-}" != "[]" ]; then
        local python_cmd="${PYTHON:-python}"
        if ! command -v "$python_cmd" >/dev/null 2>&1; then
            if command -v python3 >/dev/null 2>&1; then
                python_cmd="python3"
            else
                echo "Error: python is required to restrict submit to KNOWN DEFECT FILES."
                return 2
            fi
        fi
        if ! defect_filter_file="$(mktemp)"; then
            echo "Error: failed to create submit defect-filter temp file."
            return 2
        fi
        previous_exit_trap="$(trap -p EXIT)"
        trap '_submit_cleanup_filter' EXIT
        "$python_cmd" - <<'PY' > "$defect_filter_file"
import json
import os

def normalize(value):
    value = str(value or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    parts = []
    for part in value.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)

try:
    items = json.loads(os.environ.get("MSWE_DEFECT_FILES_JSON") or "[]")
except json.JSONDecodeError:
    items = []

project = normalize(os.environ.get("MSWE_PROJECT_PATH", ""))
allowed = set()
for item in items:
    normalized = normalize(item)
    if not normalized:
        continue
    allowed.add(normalized)
    if project and project != "." and normalized.startswith(project + "/"):
        allowed.add(normalized[len(project) + 1 :])

for path in sorted(allowed):
    print(path)
PY
        if [ $? -ne 0 ]; then
            echo "Error: failed to build the submit defect-file filter."
            _submit_cleanup_filter
            return 2
        fi
    fi

    _submit_matches_defect_filter() {
        local path="$1"
        if [ -z "$defect_filter_file" ]; then
            return 0
        fi
        grep -Fxq -- "$path" "$defect_filter_file"
    }

    local submit_paths=()
    _submit_add_path() {
        local path="$1"
        [ -z "$path" ] && return
        if _submit_is_blocked_artifact_path "$path"; then
            return
        fi
        if ! _submit_is_eligible_code_path "$path"; then
            return
        fi
        if ! _submit_matches_defect_filter "$path"; then
            return
        fi
        local existing
        for existing in "${submit_paths[@]}"; do
            [ "$existing" = "$path" ] && return
        done
        submit_paths+=("$path")
    }

    # Check if the patch file exists and is non-empty
    if [ -s "$HOME/test.patch" ] || [ -s "/root/test.patch" ]; then
        # Apply the patch in reverse
        if [ -s "$HOME/test.patch" ]; then
            git apply -R < "$HOME/test.patch"
        else
            git apply -R < "/root/test.patch"
        fi
    fi

    git reset -q
    local diff_base_args=()
    if [ -n "${MSWE_NATIVE_ENV_BASE_TREE:-}" ]; then
        diff_base_args=("${MSWE_NATIVE_ENV_BASE_TREE}")
    fi
    local path
    while IFS= read -r path; do
        _submit_add_path "$path"
    done < <(git diff --name-only --diff-filter=ACMRT "${diff_base_args[@]}" --)
    while IFS= read -r path; do
        _submit_add_path "$path"
    done < <(git ls-files --others --exclude-standard)

    if [ ${#submit_paths[@]} -eq 0 ]; then
        echo "Error: no eligible .ets/.ts changes to submit after filtering generated artifacts."
        if [ -n "$defect_filter_file" ]; then
            echo "KNOWN DEFECT FILES considered for submit:"
            cat "$defect_filter_file"
        fi
        echo "Changed files seen by git:"
        git diff --name-only --diff-filter=ACMRT "${diff_base_args[@]}" --
        git ls-files --others --exclude-standard
        _submit_cleanup_filter
        return 2
    fi

    if ! git add -- "${submit_paths[@]}"; then
        echo "Error: failed to stage one or more auto-collected submit paths."
        _submit_cleanup_filter
        return 2
    fi
    # rm binaries files
    _rm_binaries
    if [ -n "${MSWE_NATIVE_ENV_BASE_TREE:-}" ]; then
        git diff --cached "${MSWE_NATIVE_ENV_BASE_TREE}" -- "${submit_paths[@]}" > model.patch
    else
        git diff --cached -- "${submit_paths[@]}" > model.patch
    fi
    if [ ! -s model.patch ]; then
        echo "Error: model.patch is empty for the auto-collected .ets/.ts submit paths."
        _submit_cleanup_filter
        return 2
    fi
    echo "Auto-collected submit paths:"
    printf '%s\n' "${submit_paths[@]}"
    if [ -n "${MSWE_NATIVE_REPO_ROOT:-}" ]; then
        echo "<<SUBMISSION_FILE||$ROOT/model.patch||SUBMISSION_FILE>>"
    else
        echo "<<SUBMISSION||"
        cat model.patch
        echo "||SUBMISSION>>"
    fi
    _submit_cleanup_filter
}

# @yaml
# signature: repair_status
# docstring: shows current changed .ets/.ts files, unmodified defect files, and outside-defect code edits
repair_status() {
    (
        cd "$ROOT" || exit 1
        python E:/WorkApp/arkagent/command_line_tools_test/tools/repair_status.py \
            --repo-path . \
            --defect-files-json "${MSWE_DEFECT_FILES_JSON:-[]}"
    )
}

# @yaml
# signature: ohpm <args>
# docstring: Route ohpm execution directly to native Windows powershell
# arguments:
ohpm() {
    local args=""
    for arg in "$@"; do
        args="$args \"$arg\""
    done
    powershell.exe -Command ". E:\WorkApp\MSWE-agent\MSWE-agent\MSWE-agent\setup-deveco-env.ps1; ohpm.bat $args"
}
