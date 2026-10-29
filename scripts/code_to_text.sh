#!/bin/bash

# A script to traverse a codebase, generate a tree view, and concatenate
# the contents of all text files into a single output file.
#
# v3: Rewritten to use arrays for command construction, avoiding `eval`
#     and fixing shell syntax errors.

set -o pipefail

# --- Configuration ---
SEPARATOR="--------------------------------------------------------------------------------"

# --- Functions ---
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS] <codebase_folder> <output_file>

Generates a single text file representing the structure and content of a codebase.

ARGUMENTS:
  <codebase_folder>     The path to the folder to process.
  <output_file>         The path to the output text file.

OPTIONS:
  -e, --exclude <pattern>  Exclude files or directories matching the pattern.
                           Can be used multiple times. Globs like '*' are supported.
                           Example: -e '.git' -e 'node_modules' -e '*.lock'
  -i, --include <pattern>  Only include files or directories matching the pattern.
                           If used, all non-matching files are skipped.
                           Can be used multiple times. Example: -i '*.py'
  -h, --help               Display this help message and exit.

EXAMPLES:
  # Basic usage
  $(basename "$0") ./my-project project_context.txt

  # Exclude the .git and node_modules directories and all .lock files
  $(basename "$0") -e '.git' -e 'node_modules' -e '*.lock' ./my-project project_context.txt
EOF
  exit 1
}

check_deps() {
  if ! command -v tree &>/dev/null; then
    echo "Warning: 'tree' command not found. Using a simpler fallback for the file tree."
    echo "For the best output, please install it (e.g., 'brew install tree')."
    USE_TREE=false
  else
    USE_TREE=true
  fi
}

# --- Argument Parsing ---
EXCLUDE_PATTERNS=()
INCLUDE_PATTERNS=()

while [[ "$#" -gt 0 ]]; do
  case $1 in
  -e | --exclude) EXCLUDE_PATTERNS+=("$2"); shift ;;
  -i | --include) INCLUDE_PATTERNS+=("$2"); shift ;;
  -h | --help) usage ;;
  -*) echo "Unknown option: $1"; usage ;;
  *) break ;;
  esac
  shift
done

# --- Input Validation ---
CODEBASE_DIR=$1
OUTPUT_FILE=$2

if [[ -z "$CODEBASE_DIR" || -z "$OUTPUT_FILE" ]]; then
  echo "Error: Missing required arguments." >&2; usage
fi
if [[ ! -d "$CODEBASE_DIR" ]]; then
  echo "Error: Codebase folder '$CODEBASE_DIR' not found." >&2; exit 1
fi
if [[ "$OUTPUT_FILE" != /* ]]; then
  OUTPUT_FILE="$(pwd)/$OUTPUT_FILE"
fi
if [[ -d "$OUTPUT_FILE" ]]; then
  echo "Error: Output file path '$OUTPUT_FILE' is a directory." >&2; exit 1
fi
OUTPUT_DIR=$(dirname "$OUTPUT_FILE")
if ! mkdir -p "$OUTPUT_DIR"; then
  echo "Error: Could not create output directory '$OUTPUT_DIR'." >&2; exit 1
fi

# --- Main Logic ---
check_deps
pushd "$CODEBASE_DIR" >/dev/null || exit

echo "Processing folder: $(pwd)"
echo "Output will be saved to: $OUTPUT_FILE"
> "$OUTPUT_FILE"

# 1. Generate the directory tree structure
echo "Generating file tree..."
if $USE_TREE; then
  exclude_string=$(IFS='|'; echo "${EXCLUDE_PATTERNS[*]}")
  if [ -n "$exclude_string" ]; then
    tree -a --dirsfirst -I "$exclude_string" >> "$OUTPUT_FILE"
  else
    tree -a --dirsfirst >> "$OUTPUT_FILE"
  fi
else
  # Fallback find-based tree
  find_tree_args=(. -type d)
  if [ ${#EXCLUDE_PATTERNS[@]} -gt 0 ]; then
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
      find_tree_args+=(-not -path "*/${pattern}/*" -and -not -name "$pattern")
    done
  fi
  { find . -print | sort | sed '1d;s,[^/]*\/,|   ,g;s,/[^/]*$,|-- &,'; } >> "$OUTPUT_FILE"
fi
echo -e "\n\n" >>"$OUTPUT_FILE"

# 2. Build the find command using arrays for safety
echo "Locating and appending file contents..."
find_args=(. -type f)

# Add exclusions to the find command
if [ ${#EXCLUDE_PATTERNS[@]} -gt 0 ]; then
    exclude_args=()
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        exclude_args+=(-not -path "*/${pattern}/*" -and -not -name "$pattern")
    done
    find_args+=("${exclude_args[@]}")
fi

# Add inclusions to the find command
if [ ${#INCLUDE_PATTERNS[@]} -gt 0 ]; then
    include_args=()
    for pattern in "${INCLUDE_PATTERNS[@]}"; do
        if [ ${#include_args[@]} -gt 0 ]; then
            include_args+=(-o)
        fi
        include_args+=(-name "$pattern")
    done
    find_args+=(-a \( "${include_args[@]}" \))
fi

processed_count=0
# Execute the find command and process each file
while IFS= read -r file_path; do
  mime_type=$(file -b --mime-type "$file_path")
  if [[ "$mime_type" == text/* || "$mime_type" == application/*json || "$mime_type" == application/*xml || "$mime_type" == application/*yaml || "$mime_type" == "inode/x-empty" ]]; then
    clean_path="${file_path#./}"
    # Append separator and file header
    echo "$SEPARATOR" >>"$OUTPUT_FILE"
    echo "/$clean_path:" >>"$OUTPUT_FILE"
    echo "$SEPARATOR" >>"$OUTPUT_FILE"
    # Append file content with line numbers, handling empty files gracefully
    if [[ -s "$file_path" ]]; then
      nl -w 4 -s ' | ' "$file_path" >>"$OUTPUT_FILE"
    else
      echo "[EMPTY FILE]" >>"$OUTPUT_FILE"
    fi
    echo -e "\n\n" >>"$OUTPUT_FILE"
    ((processed_count++))
  else
    echo "Skipping binary file: ${file_path#./} ($mime_type)"
  fi
done < <(find "${find_args[@]}" | sort)

popd >/dev/null
echo "✅ Done. Processed $processed_count text files."
echo "Output file is available at: $OUTPUT_FILE"
