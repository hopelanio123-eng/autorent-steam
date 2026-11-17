import argparse
import socket
from threading import Event, Thread
import webbrowser

import uvicorn

from app.main import app


def serve(port: int, *, open_browser: bool) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}/"
        server = uvicorn.Server(uvicorn.Config(
            app, http="h11", loop="asyncio", ws="none", lifespan="on", access_log=False,
        ))
        stop = Event()

        def show_panel():
            while not stop.wait(0.1):
                if server.started:
                    webbrowser.open(address)
                    return

        print(f"AutoRent Steam: {address}", flush=True)
        if open_browser:
            Thread(target=show_panel, daemon=True).start()
        try:
            server.run(sockets=[listener])
        finally:
            stop.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AutoRent Steam local panel")
    parser.add_argument("--port", type=int, default=8765, metavar="PORT")
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    if not 0 <= options.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        serve(options.port, open_browser=not options.no_browser)
    except OSError as error:
        if getattr(error, "winerror", None) == 10048:
            parser.exit(1, f"Port {options.port} is already in use. Try --port 8766.\n")
        raise
