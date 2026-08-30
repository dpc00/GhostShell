"""Helper process for the live broker test; intentionally killed by its parent."""

import os
from pathlib import Path
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sublime_stub import install as _install_stubs  # noqa: E402

_install_stubs()

import ai_terminal  # noqa: E402


def main():
    pipe, marker, ready_path = sys.argv[1:]
    client = ai_terminal._BrokerPty(
        pipe,
        [os.environ.get("COMSPEC", "cmd.exe")],
        str(ROOT),
        90,
        30,
        os.environ.copy(),
        allow_spawn=False,
    )
    client.start()
    threading.Thread(target=client.read, args=(lambda _data: None,), daemon=True).start()
    client.write(("set GHOSTSHELL_TEST_STATE=" + marker + "\r").encode())
    Path(ready_path).write_text("ready", encoding="ascii")
    threading.Event().wait()


if __name__ == "__main__":
    main()
