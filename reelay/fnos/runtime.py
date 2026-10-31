"""One container, two upstream processes; either failing restarts the container."""
import signal
import subprocess
import sys
import time


def main():
    from ..core import paths
    if not paths.CONFIG.exists():
        import runpy
        runpy.run_path('/app/init_fnos.py', run_name='__main__')
    commands = [[sys.executable, '-c', 'from reelay.web.app import main; main()'],
                [sys.executable, '-m', 'reelay.main']]
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(command))
        while not stopping and all(c.poll() is None for c in children):
            time.sleep(.5)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0 if all(c.returncode == 0 for c in children) else 1


if __name__ == '__main__':
    sys.exit(main())
