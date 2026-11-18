import subprocess
import sys
from pathlib import Path

from launcher import main


def start():
    root = Path(__file__).resolve().parent
    for folder in ('.venv', 'venv'):
        executable = root / folder / 'Scripts' / 'pythonw.exe'
        if executable.is_file():
            if executable.resolve() == Path(sys.executable).resolve():
                break
            subprocess.Popen([str(executable), str(root / 'launcher.py'), '--gui'], cwd=root)
            return 0
    return main(['--gui'])


if __name__ == '__main__':
    raise SystemExit(start())
