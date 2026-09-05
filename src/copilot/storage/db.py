"""Database connection management."""

import psycopg2
from psycopg2.extras import RealDictCursor

from copilot.config import settings


def get_conn():
    return psycopg2.connect(settings.database_url, cursor_factory=RealDictCursor)
