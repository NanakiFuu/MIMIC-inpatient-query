"""Verify installed project wheel independently from repository PYTHONPATH."""
from importlib.metadata import version
from pathlib import Path
import json
import medical_query
from medical_query.api import app,STATIC


def main():
    assert version('mimic-inpatient-query')==medical_query.__version__==app.version=='0.3.0'
    assert all((STATIC/name).is_file() for name in ['index.html','app.js','style.css'])
    result={'installed_package_version':version('mimic-inpatient-query'),'api_version':app.version,
            'static_assets_present':True,'installed_location':str(Path(medical_query.__file__).parent),
            'scope':'Independent wheel installation outside project PYTHONPATH; no model call or DB mutation','passed':True}
    print(json.dumps(result))


if __name__=='__main__':
    main()
