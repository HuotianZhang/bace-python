"""Double-click to start the BACE service and open its console.

The service runs without a window and writes its log to runs/logs/. Closing
the console leaves it running; `shut down` in the console parks the bench and
ends it. scripts/Run Service.bat starts the same service in a terminal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bace.launcher import main  # noqa: E402

sys.exit(main())
