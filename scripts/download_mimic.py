"""Download only the seven official Demo2.2 tables; verify official SHA256.

Data is stored in an ignored directory and is never bundled with source code.
"""
import argparse
import hashlib
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import subprocess

BASE='https://physionet.org/files/mimic-iv-demo/2.2/'
FILES=['hosp/patients.csv.gz','hosp/admissions.csv.gz','hosp/d_icd_diagnoses.csv.gz',
       'hosp/d_labitems.csv.gz','hosp/diagnoses_icd.csv.gz','hosp/labevents.csv.gz','icu/icustays.csv.gz']


def fetch(name,path):
    request=Request(BASE+name,headers={'User-Agent':'MIMIC-Demo-reproduction/0.3.0'})
    with urlopen(request,timeout=60) as response, path.open('wb') as output:
        declared=response.headers.get('Content-Length')
        received=0
        while block:=response.read(1024*1024):
            output.write(block)
            received+=len(block)
    # A dropped connection ends the read loop without raising, so a truncated
    # body would otherwise be accepted as success. Compare against the declared
    # length when the server sends one.
    if declared is not None and received!=int(declared):
        raise OSError(f'truncated transfer for {name}: {received} of {declared} bytes')


def download(name,path,attempts=5):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    for attempt in range(1,attempts+1):
        try:
            try:
                fetch(name,temporary)
            except HTTPError as error:
                if error.code!=403:
                    raise
                error.close()
                # Retry the same official public HTTPS URL with another standard client.
                subprocess.run(['curl','--fail','--location','--retry','2','--max-time','600',
                                BASE+name,'--output',str(temporary)],check=True)
            temporary.replace(path)
            return
        except (OSError,subprocess.CalledProcessError) as error:
            temporary.unlink(missing_ok=True)
            if attempt==attempts:
                raise
            print(f'  retry {attempt}/{attempts} for {name}: {error}',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,default=Path(__file__).resolve().parents[1]/'mimic-iv-clinical-database-demo-2.2')
    args=parser.parse_args()
    base=args.directory.resolve()
    base.mkdir(parents=True,exist_ok=True)
    for name in ['SHA256SUMS.txt','LICENSE.txt']:
        if not (base/name).exists():
            download(name,base/name)
    expected={}
    for line in (base/'SHA256SUMS.txt').read_text().splitlines():
        digest,name=line.split(maxsplit=1)
        expected[name.lstrip('*')]=digest
    for name in FILES:
        path=base/name
        for attempt in range(1,6):
            if not path.exists():
                download(name,path)
            with path.open('rb') as stream:
                actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual==expected.get(name):
                break
            path.unlink()
            print(f'  hash mismatch for {name}, re-downloading (attempt {attempt})',flush=True)
        else:
            raise SystemExit('Official hash mismatch after retries; bad file removed: '+name)
        print('verified',name,flush=True)


if __name__=='__main__':
    main()
