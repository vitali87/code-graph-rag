#!/usr/bin/env bash
set -euo pipefail

msg_file="$1"
first_line=$(head -1 "$msg_file")
line_count=$(grep -c '.' "$msg_file" 2>/dev/null || echo "0")

if [ "$line_count" -gt 1 ]; then
    echo "ERROR: Commit message must be a single line."
    echo "Found $line_count non-empty lines."
    echo ""
    echo "Your message:"
    cat "$msg_file"
    exit 1
fi

if [ -z "$first_line" ]; then
    echo "ERROR: Commit message cannot be empty."
    exit 1
fi

exit 0
