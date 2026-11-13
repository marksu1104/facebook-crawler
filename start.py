"""Create the local environment and launch the app on Windows, macOS, and Linux."""
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def prepare_environment(root):
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        if environment.exists():
            raise RuntimeError("The .venv folder is incomplete or belongs to another OS. Rename it and try again.")
        print("Setting up Social Watch (first launch)...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    requirements = root / "requirements.txt"
    required_hash = hashlib.sha256(requirements.read_bytes()).hexdigest().upper()
    stamp = environment / "requirements.sha256"
    installed_hash = stamp.read_text(encoding="ascii").strip() if stamp.is_file() else ""
    if required_hash != installed_hash:
        print("Installing app dependencies...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(requirements)], check=True)
        stamp.write_text(required_hash + "\n", encoding="ascii")
    return python


def main():
    if sys.version_info < (3, 10):
        print("Python 3.10+ is required. Install a newer Python and try again.", file=sys.stderr)
        return 1
    try:
        python = prepare_environment(ROOT)
        return subprocess.call([str(python), str(ROOT / "verify.py"), *sys.argv[1:]], cwd=ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Could not start Social Watch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
