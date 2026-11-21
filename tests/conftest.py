"""Network-free default for the new live reviewer; its tests explicitly enable a mock."""

import os

os.environ["EMAIL_REVIEW_ENABLED"] = "false"
