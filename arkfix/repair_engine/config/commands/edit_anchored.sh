# @yaml
# signature: |-
#   edit_anchored [<min_start_line>]
#   <<<START
#   <first line of region to replace — exact text including indentation>
#   <<<END
#   <last line of region to replace — exact text including indentation>
#   <<<
#   <replacement lines>
#   end_of_edit_anchored
# docstring: Replaces the contiguous block from the first line that exactly matches the START line through the first matching END line on or after it (inclusive). Fails without modifying the file if the (START,END) region is not uniquely found — use optional min_start_line (1-based) to search only from that line downward when anchors repeat. Same post-edit checks and rollback behavior as edit (flake8 / node --check / optional TS strip-types). Prefer this over raw line-number edit when the file may shift.
# end_name: end_of_edit_anchored
# arguments:
#   min_start_line:
#     type: integer
#     description: if provided, only consider START matches on this line or below (disambiguates duplicate anchors)
#     required: false
#   start_anchor_line:
#     type: string
#     description: exact first line of the region to replace
#     required: true
#   end_anchor_line:
#     type: string
#     description: exact last line of the region to replace (inclusive)
#     required: true
#   replacement_text:
#     type: string
#     description: new content for the region (may be empty to delete the block)
#     required: true
edit_anchored() {
    if [ -z "$CURRENT_FILE" ]; then
        echo 'No file open. Use the `open` command first.'
        return
    fi

    local min_sl=1
    local re='^[0-9]+$'
    if [[ -n "${1:-}" ]]; then
        if [[ "$1" =~ $re ]]; then
            min_sl=$1
        else
            echo "Usage: edit_anchored [<min_start_line>]"
            echo "Optional first argument must be a positive line number, or omit it and put <<<START on the first body line."
            return
        fi
    fi

    local phase_line=""
    read -r phase_line || true
    phase_line="${phase_line//$'\r'/}"
    if [[ "$phase_line" =~ $re ]]; then
        min_sl=$phase_line
        read -r phase_line || true
        phase_line="${phase_line//$'\r'/}"
    fi
    if [[ "$phase_line" != "<<<START" ]]; then
        echo "Protocol error: expected <<<START (or optional <min_start_line> then <<<START) as the first line(s) after edit_anchored."
        echo "Got: ${phase_line:-<empty>}"
        return
    fi

    local start_anchor=""
    read -r start_anchor || true
    start_anchor="${start_anchor//$'\r'/}"
    local mk_end=""
    read -r mk_end || true
    mk_end="${mk_end//$'\r'/}"
    if [[ "$mk_end" != "<<<END" ]]; then
        echo "Protocol error: expected <<<END after the START anchor line."
        echo "Got: ${mk_end:-<empty>}"
        return
    fi

    local end_anchor=""
    read -r end_anchor || true
    end_anchor="${end_anchor//$'\r'/}"
    local mk_rep=""
    read -r mk_rep || true
    mk_rep="${mk_rep//$'\r'/}"
    if [[ "$mk_rep" != "<<<" ]]; then
        echo "Protocol error: expected <<< after the END anchor line."
        echo "Got: ${mk_rep:-<empty>}"
        return
    fi

    local line_count=0
    local replacement=()
    while IFS= read -r line; do
        line="${line//$'\r'/}"
        if [[ "$line" == "end_of_edit_anchored" ]]; then
            break
        fi
        replacement+=("$line")
        ((line_count++))
    done

    local rep_tmp="$HOME/.edit_rep_tmp_${RANDOM}"
    printf "%s\n" "${replacement[@]}" > "$rep_tmp"

    local py_script="$HOME/.edit_script_${RANDOM}.py"
    cat << 'EOF_PYTHON' > "$py_script"
import sys, os
min_sl = int(sys.argv[1])
start_anchor = sys.argv[2]
end_anchor = sys.argv[3]
current_file = sys.argv[4]
rep_file = sys.argv[5]

try:
    with open(current_file, 'r', encoding='utf-8') as f:
        lines = [l.strip('\r\n') for l in f.readlines()]
except Exception as e:
    print(f"Error reading {current_file}: {e}")
    sys.exit(1)

with open(rep_file, 'r', encoding='utf-8') as f:
    rep_lines = [l.strip('\r\n') for l in f.readlines()]

matches = []
for i in range(min_sl - 1, len(lines)):
    if lines[i] == start_anchor:
        for j in range(i, len(lines)):
            if lines[j] == end_anchor:
                matches.append((i, j))
                break

if len(matches) == 0:
    print("edit_anchored: no region found where START then END match exactly.")
    print("Hint: copy START/END lines from a fresh \`open\` view; try optional min_start_line if anchors repeat above the target.")
    sys.exit(2)
if len(matches) > 1:
    print(f"edit_anchored: ambiguous — {len(matches)} candidate regions match START/END.")
    print("Pass <min_start_line> (1-based) so only the intended occurrence is considered (e.g. line from \`open\`).")
    sys.exit(2)

s_idx, e_idx = matches[0]
new_lines = lines[:s_idx] + rep_lines + lines[e_idx+1:]

with open(current_file, 'w', encoding='utf-8', newline='\n') as f:
    for line in new_lines:
        f.write(line + '\n')

print(f"{s_idx} {e_idx+1}")
EOF_PYTHON

    cp "$CURRENT_FILE" "$HOME/$(basename "$CURRENT_FILE")_backup"

    local py_out
    py_out=$(python "$py_script" "$min_sl" "$start_anchor" "$end_anchor" "$CURRENT_FILE" "$rep_tmp" 2>&1)
    local ret=$?
    
    rm -f "$py_script" "$rep_tmp"

    if [ $ret -ne 0 ]; then
        echo "$py_out"
        rm -f "$HOME/$(basename "$CURRENT_FILE")_backup"
        return
    fi

    local start_line end_line
    read -r start_line end_line <<< "$py_out"

    local lint_output
    lint_output="$(_post_edit_lint_output "$CURRENT_FILE")"

    if [ -z "$lint_output" ]; then
        export CURRENT_LINE=$start_line
        _constrain_line
        _print
        echo "File updated (anchored edit). Review indentation and structure; use edit_anchored again or git checkout -- <file> if needed."
    else
        echo "Your proposed anchored edit failed post-edit checks. See errors below; the file was restored."
        echo ""
        echo "ERRORS:"
        _split_string "$lint_output"
        echo ""

        original_current_line=$CURRENT_LINE
        original_window=$WINDOW

        export CURRENT_LINE=$(( (line_count / 2) + start_line ))
        export WINDOW=$((line_count + 10))

        echo "This is how your edit would have looked if applied"
        echo "-------------------------------------------------"
        _constrain_line
        _print
        echo "-------------------------------------------------"
        echo ""

        cp "$HOME/$(basename "$CURRENT_FILE")_backup" "$CURRENT_FILE"

        export CURRENT_LINE=$(( ((end_line - start_line + 1) / 2) + start_line ))
        export WINDOW=$((end_line - start_line + 10))

        echo "This is the original code before your edit"
        echo "-------------------------------------------------"
        _constrain_line
        _print
        echo "-------------------------------------------------"

        export CURRENT_LINE=$original_current_line
        export WINDOW=$original_window

        echo "Your changes have NOT been applied."
        echo "Fix anchors/replacement or run \`git checkout -- <path>\` if the file looks corrupted, then retry."
    fi

    rm -f "$HOME/$(basename "$CURRENT_FILE")_backup"
}
