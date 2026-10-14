"""Fixed project database endpoint; secrets never enter logs or HTTP errors."""
import json
from pathlib import Path
import os

import psycopg
from psycopg.rows import dict_row


def connect():
    if 'MEDICAL_QUERY_DB_PASSWORD' in os.environ:
        config = {'host':os.environ.get('MEDICAL_QUERY_DB_HOST','127.0.0.1'),
                  'port':int(os.environ.get('MEDICAL_QUERY_DB_PORT','5432')),
                  'dbname':'medical_query_demo','user':'medical_query_reader',
                  'password':os.environ['MEDICAL_QUERY_DB_PASSWORD']}
        if not config['password']:
            raise ValueError('Database credential missing')
    else:
        config_path = Path(os.environ.get("MEDICAL_QUERY_DB_CONFIG", str(Path.home() / '.config/medical-query/database.json')))
        config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = {"host", "port", "dbname", "user", "password"}
    if set(config) != allowed or config["dbname"] != "medical_query_demo" or config["user"] != "medical_query_reader":
        raise ValueError("Unexpected application database configuration")
    return psycopg.connect(**config, connect_timeout=2, row_factory=dict_row,
                           application_name="medical-query-api",
                           options="-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000")
