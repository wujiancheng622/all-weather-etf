"""Run the local unittest suite with real network/mail blocked. No gateway access."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('outputs/offline-tests.json'))
    parser.add_argument('--pattern', default='test*.py')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    sys.dont_write_bytecode = True
    attempts = []

    def guard(event, _args):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                     'socket.bind', 'smtplib.connect', 'smtplib.send'}:
            attempts.append(event)
            raise RuntimeError('OFFLINE_TEST_NETWORK_OR_MAIL_FORBIDDEN: ' + event)

    sys.addaudithook(guard)
    os.chdir(root)
    sys.path.insert(0, str(root))
    import numpy
    import pandas
    start = time.monotonic()
    suite = unittest.defaultTestLoader.discover('tests', pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = dict(python=sys.version.split()[0], platform=sys.platform,
                   numpy=numpy.__version__, pandas=pandas.__version__,
                   pattern=args.pattern, run=result.testsRun,
                   failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), network_or_mail_attempts=attempts,
                   seconds=round(time.monotonic() - start, 3),
                   failed_cases=[str(t) for t, _ in result.failures + result.errors])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__ == '__main__':
    raise SystemExit(main())
