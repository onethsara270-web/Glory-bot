"""Entry point for Pterodactyl / any host. Startup file: main.py (app.py also works)."""
import glob
import importlib
import os
import subprocess
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)


def _add_lib_paths() -> None:
    pats = [os.path.join(BASE, ".pylibs"), os.path.join(BASE, ".local", "lib", "python*", "site-packages")]
    for pat in pats:
        for p in glob.glob(pat):
            if p not in sys.path:
                sys.path.insert(0, p)
    importlib.invalidate_caches()


def _ensure_deps() -> None:
    _add_lib_paths()
    try:
        import httpx, pydantic_settings, sqlalchemy, aiosqlite, telegram  # noqa: F401,E401
        return
    except ImportError:
        pass
    print("Installing dependencies (first start only)…", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--target",
                           os.path.join(BASE, ".pylibs"), "-r", os.path.join(BASE, "requirements.txt")])
    _add_lib_paths()


if __name__ == "__main__":
    try:
        _ensure_deps()
        from glorybot.bot import run
        run()
    except SystemExit:
        raise
    except Exception:
        print("❌ Bot failed to start:\n" + traceback.format_exc(), flush=True)
        sys.exit(1)
