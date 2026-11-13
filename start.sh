#!/bin/sh
set -eu
project_path=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
for python_command in python3 python; do
    if command -v "$python_command" >/dev/null 2>&1 &&
        "$python_command" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$python_command" "$project_path/start.py" "$@"
    fi
done
printf '%s\n' 'Python 3.10+ is required. Install Python and try again.' >&2
exit 1
