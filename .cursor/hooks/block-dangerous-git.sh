#!/bin/bash
# Block destructive git commands before execution

COMMAND="$1"

if echo "$COMMAND" | grep -qE "git (push --force|push -f|reset --hard|clean -fd|rebase --onto)"; then
    echo "BLOCKED: Destructive git command detected: $COMMAND" 
    echo "If you really intend this, run it manually in the terminal."
    exit 1
fi

exit 0
