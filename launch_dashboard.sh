#!/usr/bin/env bash
#
# Launch the health dashboard in a full-screen, chrome-less browser window
# (no tabs, no address bar). Starts the Streamlit server if it isn't already
# running, waits for it, then opens it in Chrome's "app" mode.
#
# Usage:  ./launch_dashboard.sh
#
set -euo pipefail

cd "$(dirname "$0")"

PORT=8501
URL="http://localhost:${PORT}"

# --- Start the server if nothing is answering on the port ---
if ! curl -s -o /dev/null "$URL"; then
    echo "Starting dashboard server on ${URL} ..."
    if [ -x ".venv/bin/streamlit" ]; then
        STREAMLIT=".venv/bin/streamlit"
    else
        STREAMLIT="streamlit"
    fi
    nohup "$STREAMLIT" run dashboard.py \
        --server.headless true --server.port "$PORT" \
        >/tmp/health_dashboard.log 2>&1 &

    # Wait (up to ~20s) for it to come up
    for _ in $(seq 1 40); do
        if curl -s -o /dev/null "$URL"; then break; fi
        sleep 0.5
    done
fi

# --- Open in a chrome-less app window ---
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

if [ -x "$CHROME" ]; then
    BROWSER="$CHROME"
elif [ -x "$BRAVE" ]; then
    BROWSER="$BRAVE"
elif [ -x "$EDGE" ]; then
    BROWSER="$EDGE"
else
    BROWSER=""
fi

if [ -n "$BROWSER" ]; then
    # --app gives a window with no tabs/omnibox; --start-fullscreen maximizes it.
    # A dedicated profile dir guarantees a fresh app window even if the browser
    # is already running.
    "$BROWSER" \
        --app="$URL" \
        --start-fullscreen \
        --user-data-dir="${HOME}/.health-dashboard-kiosk" \
        >/dev/null 2>&1 &
    echo "Opened dashboard in app mode. Press Ctrl+Cmd+F (or F11 style) to toggle fullscreen; Cmd+Q to quit."
else
    echo "No Chromium-based browser found. Opening in your default browser."
    echo "Tip: press F11 (Ctrl+Cmd+F on macOS) to go fullscreen and hide tabs."
    open "$URL"
fi
