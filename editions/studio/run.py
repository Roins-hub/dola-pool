"""Start the optional Studio edition with its own working directory and port."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()

    studio = Path(__file__).resolve().parent
    os.chdir(studio)
    sys.path.insert(0, str(studio))
    os.environ['DOLA_HOST'] = args.host
    os.environ['DOLA_PORT'] = str(args.port)

    import uvicorn
    uvicorn.run('server:app', host=args.host, port=args.port)


if __name__ == '__main__':
    main()
