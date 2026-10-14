"""Load pg_stat_statements on project cluster; preserve existing preload settings."""
import os
from pathlib import Path
import subprocess

from bootstrap_database import admin, literal


def main():
    if os.getuid()!=0:
        raise SystemExit('Root required')
    current=admin('SHOW shared_preload_libraries')
    libraries=[item.strip() for item in current.split(',') if item.strip()]
    if 'pg_stat_statements' not in libraries:
        libraries.append('pg_stat_statements')
        admin('ALTER SYSTEM SET shared_preload_libraries='+literal(','.join(libraries)))
        subprocess.run(['systemctl','restart','postgresql@16-main'],check=True)
    admin('CREATE EXTENSION IF NOT EXISTS pg_stat_statements', 'medical_query_demo')
    # No extension or stats privilege is granted to the application reader.
    print('pg_stat_statements loaded; project DB extension created; reader grants unchanged')
    print(admin('SHOW shared_preload_libraries'))


if __name__=='__main__':
    main()
