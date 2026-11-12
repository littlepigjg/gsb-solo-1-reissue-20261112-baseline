"""Run the local studio with Waitress; never enable the debug reloader."""
import argparse
import logging
from backend.app import create_app


def main():
    parser = argparse.ArgumentParser(description="光屿图像工作室")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(data_dir=args.data_dir)
    from waitress import serve
    print(f"Image Studio ready: http://{args.host}:{args.port}", flush=True)
    print("Press Ctrl+C to stop. Data is stored as JSON under data/json.", flush=True)
    try:
        serve(app, host=args.host, port=args.port, threads=6, max_request_body_size=26 * 1024 * 1024)
    finally:
        app.extensions["studio"]["jobs"].shutdown()
        app.extensions["studio"]["store"].close()


if __name__ == "__main__":
    main()
