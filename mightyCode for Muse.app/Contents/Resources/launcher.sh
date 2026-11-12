#!/bin/sh
# LocalCode launcher: start the bootstrap/backend if needed, open the GUI.
# This script lives in Contents/Resources/ and is run by the compiled stub
# (Contents/MacOS/LocalCode). GUI (backend) is on 8471; the first-run setup
# progress page is on 8472.
RES="$(cd "$(dirname "$0")" && pwd)"
# Keep Python from writing __pycache__ into the signed bundle (breaks the code seal).
export PYTHONDONTWRITEBYTECODE=1
GUI_URL="http://127.0.0.1:8471/"
SETUP_URL="http://127.0.0.1:8472/"

port_open() {
  /usr/bin/python3 -c "import socket;s=socket.socket();s.settimeout(0.3);exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)"
}

if port_open 8471; then
  open "$GUI_URL"
  exit 0
fi

if ! port_open 8472; then
  nohup /usr/bin/python3 "$RES/mac/bootstrap.py" >/dev/null 2>&1 &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if port_open 8472; then
      break
    fi
    sleep 0.5
  done
fi
open "$SETUP_URL"
