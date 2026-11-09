"""Run the fake Cast device on the real Cast port so a live adapter can use it.

    python tools/probe/mock_speaker.py [port] [finish_after_seconds]

Useful for exercising the installed plugin through the running gateway without
physical hardware: point the bridge at 127.0.0.1 and every reply "plays".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.mock_cast_device import MockCastDevice  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8009
    finish_after = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5
    device = MockCastDevice(finish_after=finish_after, port=port).start()
    print(f"mock Cast device listening on 127.0.0.1:{device.port} (finishes after {finish_after}s)",
          flush=True)
    try:
        while True:
            time.sleep(1)
            if device.loaded_urls:
                print(f"LOAD {device.loaded_urls[-1]}", flush=True)
                device.loaded_urls.clear()
    except KeyboardInterrupt:
        device.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
