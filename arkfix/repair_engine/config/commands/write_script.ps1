    cat << 'EOF_PYTHON' > edit_anchored.py
import sys, re

min_sl = int(sys.argv[1])
start_anchor = sys.argv[2]
end_anchor = sys.argv[3]
current_file = sys.argv[4]
replacement_file = sys.argv[5]

# Using utf-8 rigorously
with open(current_file, 'r', encoding='utf-8') as f:
    lines = [l.rstrip('\r\n') for l in f.readlines()]
    if not lines and f.tell() > 0: # handling edge case
        f.seek(0)
        lines = [l.replace('\r\n','\n').replace('\n','') for l in f.readlines()]

with open(replacement_file, 'r', encoding='utf-8') as f:
    rep_lines = [l.rstrip('\r\n') for l in f.readlines()]
    if rep_lines and rep_lines[-1] == 'end_of_edit_anchored':
        rep_lines.pop()

matches = []
for i in range(min_sl - 1, len(lines)):
    if lines[i] == start_anchor:
        for j in range(i, len(lines)):
            if lines[j] == end_anchor:
                matches.append((i, j))
                break

if len(matches) == 0:
    print("edit_anchored: no region found where START then END match exactly.")
    print("Hint: copy START/END lines from a fresh \open\ view; try optional min_start_line if anchors repeat above the target.")
    sys.exit(1)
if len(matches) > 1:
    print(f"edit_anchored: ambiguous — {len(matches)} candidate regions match START/END.")
    print("Pass <min_start_line> (1-based) so only the intended occurrence is considered (e.g. line from \open\).")
    sys.exit(1)

start_idx, end_idx = matches[0]
new_lines = lines[:start_idx] + rep_lines + lines[end_idx+1:]

with open(current_file, 'w', encoding='utf-8', newline='\n') as f:
    for line in new_lines:
        f.write(line + '\n')

print(f"SUCCESS;{start_idx};{end_idx+1};{len(rep_lines)}")
EOF_PYTHON
