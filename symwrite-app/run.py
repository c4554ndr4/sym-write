"""Run from any directory: .venv/bin/python symwrite-app/run.py."""

import argparse
import os
from pathlib import Path
import sys

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the local SymWrite editor")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--lexical",
        action="store_true",
        help="Use local word overlap instead of downloading an embedding model",
    )
    args = parser.parse_args()
    os.environ.setdefault("SYMWRITE_ORIGIN", f"http://127.0.0.1:{args.port}")
    if args.lexical:
        os.environ["SYMWRITE_RETRIEVAL"] = "lexical"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        proxy_headers=False,
    )
