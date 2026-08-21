## Cheap post-edit checks (rollback if non-empty). Extends Python flake8 to JS and optional TS/TSX/ETS.
_post_edit_lint_output() {
    local f="$1"
    local out=""
    case "$f" in
        *.py)
            out=$(flake8 --isolated --select=F821,F822,F831,E111,E112,E113,E999,E902 --ignore=E226,E241,E402,E731,F821,F822,F823,F831,F841,F999,F405 "$f" 2>&1) || true
            ;;
        *.js|*.jsx|*.mjs|*.cjs)
            if command -v node >/dev/null 2>&1; then
                out=$(node --check "$f" 2>&1) || true
            fi
            ;;
        *.ets)
            # Node's --experimental-strip-types does not recognize the .ets extension.
            # ArkTS build_app.py is the authoritative checker for these files.
            ;;
        *.ts|*.tsx)
            # Node 20+ can syntax-check TS/TSX with strip-types (ArkTS may partially match).
            if command -v node >/dev/null 2>&1 && node --help 2>&1 | grep -q -- '--experimental-strip-types'; then
                out=$(node --check --experimental-strip-types "$f" 2>&1) || true
            fi
            ;;
    esac
    printf '%s' "$out"
}

_strict_path_edit_enabled() {
    [ "${STRICT_PATH_EDIT:-0}" = "1" ] || [ "${STRICT_PATH_EDIT:-}" = "true" ] || [ "${STRICT_PATH_EDIT:-}" = "TRUE" ]
}

# @yaml
# signature: |-
#   edit <start_line>:<end_line>
#   <replacement_text>
#   end_of_edit
# docstring: legacy current-file line edit. In strict mode this command is disabled because it can edit the wrong file after another open/search command changes CURRENT_FILE. Use edit_file <path> <start_line>:<end_line> for tiny path-bound line edits, or str_replace <path> for multi-line/block edits. Strict mode also rejects large line-number edits; use str_replace for those. After the edit, the file is checked (Python flake8; JavaScript node --check; TypeScript/TSX/ETS node --experimental-strip-types when supported). If checks report errors, the file is restored from a backup.
# end_name: end_of_edit
# arguments:
#   start_line:
#     type: integer
#     description: the line number to start the edit at
#     required: true
#   end_line:
#     type: integer
#     description: the line number to end the edit at (inclusive)
#     required: true
#   replacement_text:
#     type: string
#     description: the text to replace the current selection with
#     required: true
edit() {
    if _strict_path_edit_enabled && [ "${ALLOW_UNSAFE_EDIT:-0}" != "1" ]; then
        echo "Unsafe raw edit is disabled in this environment."
        echo "Reason: edit <range> applies to CURRENT_FILE, so it can modify the wrong file after another open/search changes the editor state."
        echo "Use one of these path-bound commands instead:"
        echo "  edit_file <path> <start_line>:<end_line>"
        echo "  str_replace <path>"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    if [ -z "$CURRENT_FILE" ]
    then
        echo 'No file open. Use the `open` command first.'
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
        if ! _mswe_guard_defect_file_edit "$CURRENT_FILE" "edit"; then
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi

    local start_line="$(echo $1: | cut -d: -f1)"
    local end_line="$(echo $1: | cut -d: -f2)"

    if [ -z "$start_line" ] || [ -z "$end_line" ]
    then
        echo "Usage: edit <start_line>:<end_line>"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local re='^[0-9]+$'
    if ! [[ $start_line =~ $re ]]; then
        echo "Usage: edit <start_line>:<end_line>"
        echo "Error: start_line must be a number"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    if ! [[ $end_line =~ $re ]]; then
        echo "Usage: edit <start_line>:<end_line>"
        echo "Error: end_line must be a number"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local requested_span=$((end_line - start_line + 1))

    # Bash array starts at 0, so let's adjust
    local start_line=$((start_line - 1))
    local end_line=$((end_line))

    local line_count=0
    local replacement=()
    while IFS= read -r line
    do
        replacement+=("$line")
        ((line_count++))
    done
    if printf "%s\n" "${replacement[@]}" | grep -q $'\357\277\275'; then
        echo "edit_file: replacement text contains Unicode replacement character U+FFFD."
        echo "This usually means source text was decoded with the wrong encoding. Reopen the file and copy exact UTF-8 text; do not write corrupted Chinese/source text."
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    if _strict_path_edit_enabled && [ "${ALLOW_LARGE_EDIT:-0}" != "1" ]; then
        if [ "$requested_span" -gt 12 ] || [ "$line_count" -gt 20 ]; then
            echo "Large line-number edits are disabled in strict mode."
            echo "Reason: large range edits are fragile when line numbers drift and can corrupt ArkTS component structure."
            echo "Use str_replace <path> with a unique OLD block copied from the latest open output."
            echo "Requested old-line span: $requested_span; replacement lines: $line_count."
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi

    # Create a process-local backup so parallel workers cannot overwrite it.
    local backup_file
    if ! backup_file="$(mktemp "${TMPDIR:-/tmp}/swe-edit-backup.XXXXXX")"; then
        echo "edit_file: failed to create backup temp file."
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    local previous_exit_trap
    previous_exit_trap="$(trap -p EXIT)"
    trap 'rm -f -- "${backup_file:-}"' EXIT
    if ! cp "$CURRENT_FILE" "$backup_file"; then
        echo "edit_file: failed to back up the target file."
        rm -f -- "$backup_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    # Read the file line by line into an array
    mapfile -t lines < "$CURRENT_FILE"
    local new_lines=("${lines[@]:0:$start_line}" "${replacement[@]}" "${lines[@]:$((end_line))}")
    # Write the new stuff directly back into the original file
    if ! printf "%s\n" "${new_lines[@]}" >| "$CURRENT_FILE"; then
        echo "edit_file: failed to write the target file."
        if ! cp "$backup_file" "$CURRENT_FILE"; then
            echo "edit_file: failed to restore the target file from backup."
        fi
        rm -f -- "$backup_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    if cmp -s "$backup_file" "$CURRENT_FILE"; then
        echo "edit_file: replacement produced no file change."
        rm -f -- "$backup_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    # Run linter / syntax check
    lint_output="$(_post_edit_lint_output "$CURRENT_FILE")"

    # if there is no output, then the file is good
    local edit_result=0
    if [ -z "$lint_output" ]; then
        export CURRENT_LINE=$start_line
        _constrain_line
        _print

        echo "File updated. Please review the changes and make sure they are correct (correct indentation, no duplicate lines, etc). Edit the file again if necessary."
    else
        echo "Your proposed edit has introduced new syntax error(s). Please read this error message carefully and then retry editing the file."
        echo ""
        echo "ERRORS:"
        _split_string "$lint_output"
        echo ""

        # Save original values
        original_current_line=$CURRENT_LINE
        original_window=$WINDOW

        # Update values
        export CURRENT_LINE=$(( (line_count / 2) + start_line )) # Set to "center" of edit
        export WINDOW=$((line_count + 10)) # Show +/- 5 lines around edit

        echo "This is how your edit would have looked if applied"
        echo "-------------------------------------------------"
        _constrain_line
        _print
        echo "-------------------------------------------------"
        echo ""

        # Restoring CURRENT_FILE to original contents.
        if ! cp "$backup_file" "$CURRENT_FILE"; then
            echo "edit_file: failed to restore the target file from backup."
            edit_result=2
        else
            edit_result=1
        fi

        export CURRENT_LINE=$(( ((end_line - start_line + 1) / 2) + start_line ))
        export WINDOW=$((end_line - start_line + 10))

        echo "This is the original code before your edit"
        echo "-------------------------------------------------"
        _constrain_line
        _print
        echo "-------------------------------------------------"

        # Restore original values
        export CURRENT_LINE=$original_current_line
        export WINDOW=$original_window

        echo "Your changes have NOT been applied. Please fix your edit command and try again."
        echo "You either need to 1) Specify the correct start/end line arguments or 2) Correct your edit code."
        echo "DO NOT re-run the same failed edit command. Running it again will lead to the same error."
    fi

    rm -f -- "$backup_file"
    trap - EXIT
    [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
    if [ "$edit_result" -eq 0 ]; then
        echo "EDIT_STATUS=APPLIED"
    else
        echo "EDIT_STATUS=REJECTED"
    fi
    return "$edit_result"
}

# @yaml
# signature: |-
#   edit_file <path> <start_line>:<end_line>
#   <replacement_text>
#   end_of_edit_file
# docstring: path-bound line edit. Replaces lines <start_line> through <end_line> (inclusive) in <path>, then opens that exact file at the edited region. This is safer than legacy edit because the target file is explicit and cannot be changed by CURRENT_FILE drift. In strict mode it accepts only tiny nearby-line edits; large old ranges or large replacements are rejected. Prefer str_replace <path> for whole methods, multi-line blocks, or any edit where line numbers may drift.
# end_name: end_of_edit_file
# arguments:
#   path:
#     type: string
#     description: repo-relative path to the file to edit
#     required: true
#   range:
#     type: string
#     description: line range in the form <start_line>:<end_line>
#     required: true
#   replacement_text:
#     type: string
#     description: the text to replace the selected lines with
#     required: true
edit_file() {
    if [ $# -ne 2 ]; then
        echo "Usage: edit_file <path> <start_line>:<end_line>"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local target="$1"
    local range="$2"
    if command -v _mswe_resolve_path >/dev/null 2>&1; then
        target="$(_mswe_resolve_path "$target")"
    fi
    if [ ! -f "$target" ]; then
        echo "edit_file: file does not exist: $1"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local abs_target
    abs_target="$(realpath "$target")"
    if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
        if ! _mswe_guard_defect_file_edit "$abs_target" "edit_file"; then
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi
    export CURRENT_FILE="$abs_target"

    ALLOW_UNSAFE_EDIT=1 edit "$range"
}

# @yaml
# signature: |-
#   str_replace <path>
#   <<<<<<< OLD
#   <exact old text>
#   =======
#   <new text>
#   >>>>>>> NEW
#   end_of_str_replace
# docstring: replaces one unique text block in <path> with new text, using an OpenCode-style string replacement flow. It first tries exact old text, then safe fallback matchers for line-ending, trimmed-line, anchor, whitespace, indentation, escape, and boundary differences; the chosen match must still resolve to exactly one concrete file span or the file is left unchanged. Use this for multi-line edits, whole-block replacement, and any edit where line numbers may drift. For a newly created empty file, OLD may be empty to write the initial full file content. After the replacement, the same post-edit checks run and the file is restored from backup on syntax-check failure.
# end_name: end_of_str_replace
# arguments:
#   path:
#     type: string
#     description: repo-relative path to the file to edit
#     required: true
#   replacement_block:
#     type: string
#     description: a block with <<<<<<< OLD, =======, and >>>>>>> NEW marker lines
#     required: true
str_replace() {
    if [ $# -ne 1 ]; then
        echo "Usage: str_replace <path>"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local target="$1"
    if command -v _mswe_resolve_path >/dev/null 2>&1; then
        target="$(_mswe_resolve_path "$target")"
    fi
    if [ ! -f "$target" ]; then
        echo "str_replace: file does not exist: $1"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local abs_target
    abs_target="$(realpath "$target")"
    if command -v _mswe_guard_defect_file_edit >/dev/null 2>&1; then
        if ! _mswe_guard_defect_file_edit "$abs_target" "str_replace"; then
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi

    local python_cmd="${PYTHON:-python}"
    if ! command -v "$python_cmd" >/dev/null 2>&1; then
        if command -v python3 >/dev/null 2>&1; then
            python_cmd="python3"
        else
            echo "str_replace: python or python3 is required."
            echo "EDIT_STATUS=REJECTED"
            return 2
        fi
    fi

    local body_file="" backup_file="" line_file=""
    if ! body_file="$(mktemp)"; then
        echo "str_replace: failed to create body temp file."
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    if ! backup_file="$(mktemp)"; then
        echo "str_replace: failed to create backup temp file."
        rm -f -- "$body_file"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    if ! line_file="$(mktemp)"; then
        echo "str_replace: failed to create line temp file."
        rm -f -- "$body_file" "$backup_file"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    local previous_exit_trap
    previous_exit_trap="$(trap -p EXIT)"
    trap 'rm -f -- "${body_file:-}" "${backup_file:-}" "${line_file:-}"' EXIT

    if ! cat > "$body_file"; then
        echo "str_replace: failed to read the replacement block."
        rm -f -- "$body_file" "$backup_file" "$line_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi
    if ! cp "$abs_target" "$backup_file"; then
        echo "str_replace: failed to back up the target file."
        rm -f -- "$body_file" "$backup_file" "$line_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local py_output
    py_output=$("$python_cmd" - "$abs_target" "$body_file" "$line_file" <<'PY'
import sys
import re
from pathlib import Path

target = Path(sys.argv[1])
body_path = Path(sys.argv[2])
line_path = Path(sys.argv[3])

old_marker = "<<<<<<< OLD"
separator = "======="
new_marker = ">>>>>>> NEW"

try:
    text = target.read_text(encoding="utf-8")
except UnicodeDecodeError:
    text = target.read_text(encoding="utf-8-sig")

body = body_path.read_text(encoding="utf-8")
if "\ufffd" in body:
    print("str_replace: replacement block contains Unicode replacement character U+FFFD.")
    print("This usually means source text was decoded with the wrong encoding. Reopen the file and copy exact UTF-8 text; do not write corrupted Chinese/source text.")
    sys.exit(2)
lines = body.splitlines(keepends=True)

def marker_index(marker, start=0):
    for idx in range(start, len(lines)):
        if lines[idx].rstrip("\r\n") == marker:
            return idx
    return -1

old_idx = marker_index(old_marker)
sep_idx = marker_index(separator, old_idx + 1)
new_idx = marker_index(new_marker, sep_idx + 1)

if old_idx < 0 or sep_idx < 0 or new_idx < 0:
    print("str_replace: expected marker lines in this exact form:")
    print("<<<<<<< OLD")
    print("<exact old text>")
    print("=======")
    print("<new text>")
    print(">>>>>>> NEW")
    sys.exit(2)

trailing = "".join(lines[new_idx + 1:]).strip()
if trailing:
    print("str_replace: unexpected text after >>>>>>> NEW marker.")
    sys.exit(2)

old_str = "".join(lines[old_idx + 1:sep_idx])
new_str = "".join(lines[sep_idx + 1:new_idx])

empty_file_initial_write = False
if old_str == "":
    if text.strip():
        print("str_replace: old text is empty, but target file is not empty.")
        print("Use a unique OLD block copied from the latest open output.")
        sys.exit(2)
    empty_file_initial_write = True

def normalize_line_endings(value):
    return value.replace("\r\n", "\n")

def detect_line_ending(value):
    return "\r\n" if "\r\n" in value else "\n"

def convert_line_ending(value, ending):
    if ending == "\n":
        return value
    return value.replace("\n", "\r\n")

def occurrences(content, needle):
    if needle == "":
        return []
    result = []
    start = 0
    while True:
        idx = content.find(needle, start)
        if idx < 0:
            return result
        result.append(idx)
        start = idx + max(len(needle), 1)

def levenshtein(a, b):
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def unique(values):
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            yield value

def simple_replacer(content, find):
    yield find

def line_trimmed_replacer(content, find):
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if not search_lines:
        return
    for i in range(0, len(original_lines) - len(search_lines) + 1):
        ok = True
        for j, search_line in enumerate(search_lines):
            if original_lines[i + j].strip() != search_line.strip():
                ok = False
                break
        if ok:
            yield "\n".join(original_lines[i:i + len(search_lines)])

def block_anchor_replacer(content, find):
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return
    first = search_lines[0].strip()
    last = search_lines[-1].strip()
    candidates = []
    for i, line in enumerate(original_lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last:
                candidates.append((i, j))
                break
    if not candidates:
        return
    if len(candidates) == 1:
        i, j = candidates[0]
        yield "\n".join(original_lines[i:j + 1])
        return
    best = None
    best_score = -1.0
    for i, j in candidates:
        block = original_lines[i:j + 1]
        total = min(len(search_lines) - 2, len(block) - 2)
        if total <= 0:
            score = 1.0
        else:
            score = 0.0
            for k in range(1, total + 1):
                left = block[k].strip()
                right = search_lines[k].strip()
                max_len = max(len(left), len(right))
                score += 1.0 if max_len == 0 else 1.0 - (levenshtein(left, right) / max_len)
            score /= total
        if score > best_score:
            best_score = score
            best = (i, j)
    if best is not None and best_score >= 0.3:
        i, j = best
        yield "\n".join(original_lines[i:j + 1])

def whitespace_normalized_replacer(content, find):
    def norm(value):
        return " ".join(value.split())
    normalized_find = norm(find)
    lines = content.split("\n")
    for line in lines:
        if norm(line) == normalized_find:
            yield line
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(0, len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i:i + len(find_lines)])
            if norm(block) == normalized_find:
                yield block

def indentation_flexible_replacer(content, find):
    def remove_indent(value):
        lines = value.split("\n")
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return value
        min_indent = min(len(line) - len(line.lstrip(" \t")) for line in non_empty)
        return "\n".join(line if not line.strip() else line[min_indent:] for line in lines)
    normalized_find = remove_indent(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(0, len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i:i + len(find_lines)])
        if remove_indent(block) == normalized_find:
            yield block

def escape_normalized_replacer(content, find):
    def unescape(value):
        out = []
        i = 0
        table = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "`": "`", "\\": "\\", "$": "$"}
        while i < len(value):
            if value[i] == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                if nxt in table:
                    out.append(table[nxt])
                    i += 2
                    continue
            out.append(value[i])
            i += 1
        return "".join(out)
    unescaped = unescape(find)
    yield unescaped

def trimmed_boundary_replacer(content, find):
    trimmed = find.strip()
    if trimmed != find:
        yield trimmed
        lines = content.split("\n")
        find_lines = find.split("\n")
        for i in range(0, len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i:i + len(find_lines)])
            if block.strip() == trimmed:
                yield block

def context_aware_replacer(content, find):
    find_lines = find.split("\n")
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
    if len(find_lines) < 3:
        return
    lines = content.split("\n")
    first = find_lines[0].strip()
    last = find_lines[-1].strip()
    for i, line in enumerate(lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(lines)):
            if lines[j].strip() != last:
                continue
            block_lines = lines[i:j + 1]
            if len(block_lines) != len(find_lines):
                break
            total = 0
            matched = 0
            for k in range(1, len(block_lines) - 1):
                left = block_lines[k].strip()
                right = find_lines[k].strip()
                if left or right:
                    total += 1
                    if left == right:
                        matched += 1
            if total == 0 or matched / total >= 0.5:
                yield "\n".join(block_lines)
            break

def replace_once(content, find, replacement):
    ambiguous = False
    strategies = [
        ("exact", simple_replacer),
        ("line-trimmed", line_trimmed_replacer),
        ("block-anchor", block_anchor_replacer),
        ("whitespace-normalized", whitespace_normalized_replacer),
        ("indentation-flexible", indentation_flexible_replacer),
        ("escape-normalized", escape_normalized_replacer),
        ("trimmed-boundary", trimmed_boundary_replacer),
        ("context-aware", context_aware_replacer),
    ]
    for strategy, replacer in strategies:
        for candidate in unique(replacer(content, find)):
            hits = occurrences(content, candidate)
            if len(hits) == 1:
                idx = hits[0]
                return content[:idx] + replacement + content[idx + len(candidate):], idx, strategy
            if len(hits) > 1:
                ambiguous = True
    if ambiguous:
        raise ValueError("multiple")
    raise ValueError("missing")

def candidate_lines_for_missing_old(content, find, limit=5):
    terms = []
    for raw_line in find.split("\n"):
        stripped = raw_line.strip()
        if not stripped or stripped in ("{", "}", "};", ");", "],", "]"):
            continue
        terms.append(stripped)
        terms.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[0-9]{2,}", stripped))
    terms = list(unique(terms))
    scored = []
    for line_no, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        score = sum(1 for term in terms if term in stripped)
        if score:
            scored.append((score, line_no, stripped))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[:limit]

ending = detect_line_ending(text)
normalized_text = normalize_line_endings(text)
normalized_old = normalize_line_endings(old_str)
normalized_new = normalize_line_endings(new_str)

if empty_file_initial_write:
    new_text_normalized = normalized_new
    start = 0
    strategy = "empty-file-initial-write"
else:
    try:
        new_text_normalized, start, strategy = replace_once(normalized_text, normalized_old, normalized_new)
    except ValueError as exc:
        if str(exc) == "multiple":
            print("str_replace: old text is ambiguous after OpenCode-style matching; no changes applied.")
            print("Hint: include more surrounding context so the old block is unique.")
        else:
            print("str_replace: old text was not found by exact or safe fallback matching; no changes applied.")
            print("Hint: reopen the file and copy a larger exact block, including indentation and blank lines.")
            candidates = candidate_lines_for_missing_old(normalized_text, normalized_old)
            if candidates:
                print("Possible nearby lines from the current file:")
                for _, line_no, line in candidates:
                    if len(line) > 180:
                        line = line[:177] + "..."
                    print("  Line " + str(line_no) + ": " + line)
        sys.exit(2)

line_no = normalized_text[:start].count("\n") + 1
target.write_text(convert_line_ending(new_text_normalized, ending), encoding="utf-8", newline="")
line_path.write_text(str(line_no), encoding="ascii")
print("str_replace applied at line " + str(line_no) + " using " + strategy + " matching.")
PY
)
    local ret=$?
    if [ $ret -ne 0 ]; then
        echo "$py_output"
        if ! cp "$backup_file" "$abs_target"; then
            echo "str_replace: failed to restore the target file from backup."
            ret=2
        fi
        rm -f -- "$body_file" "$backup_file" "$line_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return $ret
    fi
    if cmp -s "$backup_file" "$abs_target"; then
        echo "$py_output"
        echo "str_replace: replacement produced no file change."
        rm -f -- "$body_file" "$backup_file" "$line_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return 2
    fi

    local lint_output
    lint_output="$(_post_edit_lint_output "$abs_target")"

    if [ -z "$lint_output" ]; then
        export CURRENT_FILE="$abs_target"
        if [ -s "$line_file" ]; then
            export CURRENT_LINE="$(cat "$line_file")"
        fi
        _constrain_line
        _print
        echo "$py_output"
        echo "File updated by exact block replacement. Reopen or review the shown region before the next edit."
    else
        echo "Your proposed str_replace has introduced new syntax error(s). The file was restored from backup."
        echo ""
        echo "ERRORS:"
        _split_string "$lint_output"
        echo ""
        local lint_result=1
        if ! cp "$backup_file" "$abs_target"; then
            echo "str_replace: failed to restore the target file from backup."
            lint_result=2
        fi
        echo "Your changes have NOT been applied. Reopen the file, copy the exact old block, and retry with corrected replacement text."
        rm -f -- "$body_file" "$backup_file" "$line_file"
        trap - EXIT
        [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
        echo "EDIT_STATUS=REJECTED"
        return "$lint_result"
    fi

    rm -f -- "$body_file" "$backup_file" "$line_file"
    trap - EXIT
    [ -n "$previous_exit_trap" ] && eval "$previous_exit_trap"
    echo "EDIT_STATUS=APPLIED"
}
