import os
import subprocess
import sys


def test_archive_dates_normalize_without_system_timezone_database():
    result = subprocess.run(
        [sys.executable, '-c', '''
from datetime import datetime
from types import SimpleNamespace
from vkdump.modules.parse_dump import _normalize_times
message = SimpleNamespace(sent_at=datetime(2024, 1, 1, 12), edited_at=datetime(2024, 1, 1, 13))
_normalize_times([message], 'Europe/Moscow')
assert message.sent_at == datetime(2024, 1, 1, 9)
assert message.edited_at == datetime(2024, 1, 1, 10)
_normalize_times([message], 'UTC')
assert message.sent_at == datetime(2024, 1, 1, 9)
'''],
        env=os.environ | {'PYTHONTZPATH': ''},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
