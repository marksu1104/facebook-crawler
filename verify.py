"""Launch only the supervised single-group pilot, without starting collection."""
import argparse
import json
import socket
import threading
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.pilot import PilotSettings, create_pilot_app


def main():
    parser = argparse.ArgumentParser(description="Local single-group verification")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--group", help="Seed an empty local pilot configuration only")
    parser.add_argument("--keywords", nargs="*", default=[])
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    with socket.socket() as sock:
        occupied = sock.connect_ex(("127.0.0.1", args.port)) == 0
    if occupied:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                existing = json.load(response)
            if existing.get("app") == "group-watch-pilot":
                if not args.no_browser:
                    webbrowser.open(url)
                print(f"Pilot already running: {url}")
                return
        except Exception:
            pass
        raise SystemExit(f"Port {args.port} is in use. Choose another port with --port.")
    app = create_pilot_app(args.data_dir)
    if args.group and not app.state.pilot.settings["group_url"]:
        app.state.pilot.save_settings(PilotSettings(group_url=args.group, keywords=args.keywords))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info"))

    def quit_server():
        threading.Timer(0.5, lambda: setattr(server, "should_exit", True)).start()
    app.state.on_quit = quit_server
    if not args.no_browser:
        def open_ready():
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                if server.should_exit:
                    return
                threading.Event().wait(0.1)
        threading.Thread(target=open_ready, daemon=True).start()
    print(f"Single-group pilot: {url}\nNo Facebook or Telegram requests start until you use the controls.", flush=True)
    server.run()


if __name__ == "__main__":
    main()
