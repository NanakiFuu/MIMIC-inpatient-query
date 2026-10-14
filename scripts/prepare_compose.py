"""Pin actually pulled image digests and create private local environment; no secret output."""
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT=Path(__file__).resolve().parents[1]
# Secrets must NOT live on the Windows mount: DrvFs does not enforce chmod, so a
# 0600 file there still shows as 0777. Keep the Compose env in a Linux-private
# directory instead. Both paths are overridable so instances can run in parallel.
PRIVATE_DIR=Path(os.environ.get('MEDICAL_QUERY_PRIVATE_DIR','/var/lib/medical-query/compose'))
ENV_FILE=Path(os.environ.get('MEDICAL_QUERY_COMPOSE_ENV',str(PRIVATE_DIR/'.env')))


def compose(*args,**kwargs):
    return subprocess.run(['docker','compose','--project-directory',str(ROOT),
                           '--env-file',str(ENV_FILE),*args],check=True,**kwargs)


def main():
    if os.getuid()!=0:
        raise SystemExit('Run with root for Docker Engine access')
    environment=ENV_FILE
    if environment.exists():
        raise SystemExit('Existing env file retained; use compose commands directly')
    images={}
    for name,tag in [('PYTHON_IMAGE','python:3.12-slim-bookworm'),('POSTGRES_IMAGE','postgres:16-bookworm')]:
        subprocess.run(['docker','pull',tag],check=True)
        inspected=json.loads(subprocess.run(['docker','image','inspect',tag],capture_output=True,text=True,check=True).stdout)[0]
        images[name]=inspected['RepoDigests'][0]
    local={'POSTGRES_PASSWORD':secrets.token_urlsafe(36),'MEDICAL_QUERY_DB_PASSWORD':secrets.token_urlsafe(36),
           'DEEPSEEK_API_KEY':'',
           'MEDICAL_QUERY_HTTP_PORT':os.environ.get('MEDICAL_QUERY_HTTP_PORT','18765'),
           'MEDICAL_QUERY_POSTGRES_PORT':os.environ.get('MEDICAL_QUERY_POSTGRES_PORT','15432'),**images}
    # Caller can supply their private model configuration; never prints it.
    path=os.environ.get('MEDICAL_QUERY_MODEL_CONFIG')
    if path:
        config=json.loads(Path(path).read_text())
        if config.get('base_url')=='https://api.deepseek.com' and config.get('model')=='deepseek-flash':
            local['DEEPSEEK_API_KEY']=config['api_key']
    environment.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(environment.parent,0o700)
    descriptor=os.open(environment,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:
        for key,value in local.items():
            if '\n' in value or '\r' in value:
                raise RuntimeError('Invalid environment value')
            stream.write(f'{key}={value}\n')
    os.chmod(environment,0o600)
    result={'pinned_images':images,'env_created':True,'credentials_printed':False,
            'env_location':str(environment),'env_permissions':oct(environment.stat().st_mode & 0o777),
            'scope':f"Separate Compose DB and volume; loopback ports {local['MEDICAL_QUERY_HTTP_PORT']}/{local['MEDICAL_QUERY_POSTGRES_PORT']}"}
    output=ROOT/'evidence/p1/docker/images.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':
    main()
