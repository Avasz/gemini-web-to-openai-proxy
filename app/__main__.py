"""``python -m app`` entrypoint."""

from __future__ import annotations

import argparse

import uvicorn

from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="gemini-openai-proxy")
    parser.add_argument("-c", "--config", help="path to JSON config file")
    parser.add_argument("--host", help="override listen host")
    parser.add_argument("--port", type=int, help="override listen port")
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = parser.parse_args()

    cfg = load_config(args.config)
    host = args.host or cfg.host
    port = args.port or int(cfg.port)

    if args.reload:
        uvicorn.run("app.main:app", host=host, port=port, reload=True)
    else:
        from .main import create_app

        uvicorn.run(create_app(cfg), host=host, port=port)


if __name__ == "__main__":
    main()
