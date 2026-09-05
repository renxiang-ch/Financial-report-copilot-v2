"""
CI-safe test configuration.

Mocks sentence_transformers so PyTorch is never imported during tests.
Real embedding calls are integration tests that require the ML extras.
"""

import sys
from unittest.mock import MagicMock

# Prevent sentence_transformers / torch from loading — not needed for unit/smoke tests
if "sentence_transformers" not in sys.modules:
    sys.modules["sentence_transformers"] = MagicMock()
