"""Scan all application image layers/config for prohibited project artifacts and actual secrets."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
ENV_FILE=Path(os.environ.get('MEDICAL_QUERY_COMPOSE_ENV','/var/lib/medical-query/compose/.env'))


def read_env():
    return dict(line.split('=',1) for line in ENV_FILE.read_text().splitlines() if line and not line.startswith('#'))


def resolve_image_parts(outer,names):
    """Return (layer_members, config_members) for docker-archive or OCI layout.

    docker save may emit the legacy layout (<id>/layer.tar + manifest.json) or the
    OCI layout (blobs/sha256/<hex> + index.json). Enumerate explicitly so we never
    silently scan zero layers and still report success.
    """
    layers=[]
    configs=[]
    if 'manifest.json' in names:
        manifest=json.loads(outer.extractfile('manifest.json').read())
        for entry in manifest:
            layers.extend(entry.get('Layers',[]))
            if entry.get('Config'):
                configs.append(entry['Config'])
    if 'index.json' in names:
        index=json.loads(outer.extractfile('index.json').read())
        for manifest_desc in index.get('manifests',[]):
            blob='blobs/sha256/'+manifest_desc['digest'].split(':')[-1]
            if blob not in names:
                continue
            sub=json.loads(outer.extractfile(blob).read())
            if sub.get('config',{}).get('digest'):
                configs.append('blobs/sha256/'+sub['config']['digest'].split(':')[-1])
            for layer in sub.get('layers',[]):
                layers.append('blobs/sha256/'+layer['digest'].split(':')[-1])
    if not layers:
        # Unknown layout: fall back to anything that looks like a layer blob.
        layers=[n for n in names if n.endswith('/layer.tar') or n.endswith('.tar') or n.startswith('blobs/sha256/')]
    return layers,configs


def main():
    if os.getuid()!=0:
        raise SystemExit('Docker access via root required')
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,
                        default=ROOT/'evidence/p1/docker/image-content-check.json',
                        help='output path; must not already exist (use a new file to re-run)')
    args=parser.parse_args()
    output=args.report
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence; pass --report with a new path')
    env=read_env()
    secret_values=[env[k].encode() for k in ['POSTGRES_PASSWORD','MEDICAL_QUERY_DB_PASSWORD','DEEPSEEK_API_KEY'] if env.get(k)]
    # Private archive is outside repository; retained for reproducible review.
    directory=Path('/var/lib/medical-query/image-review')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    archive=directory/'application.tar'
    subprocess.run(['docker','save','medical-query-demo:0.3.0','-o',str(archive)],check=True)
    os.chmod(archive,0o600)
    prohibited=[]
    secret_hits=[]
    project_files=[]
    total_files=0
    layers_scanned=0
    with tarfile.open(archive) as outer:
        names=set(m.name for m in outer.getmembers() if m.isfile())
        layers,configs=resolve_image_parts(outer,names)
        for name in configs:
            if name not in names:
                continue
            content=outer.extractfile(name).read()
            if any(secret in content for secret in secret_values):
                secret_hits.append('image_configuration:'+name)
        for name in layers:
            if name not in names:
                continue
            layers_scanned+=1
            content=outer.extractfile(name).read()
            with tarfile.open(fileobj=io.BytesIO(content)) as layer:
                for item in layer:
                    if not item.isfile():
                        continue
                    total_files+=1
                    item_name=item.name.lstrip('./')
                    base=Path(item_name).name
                    if item_name.startswith('app/'):
                        project_files.append(item_name)
                    # Check all CSVs, plus any project evidence/private data.
                    if item_name.endswith(('.csv','.csv.gz','.dump','.pem','.key')) or base in {'.env','model.json','database.json'} or item_name.startswith(('app/evidence/','app/research/','app/.local/','app/mimic-iv-')):
                        # OS certificate bundles are public, not project private keys.
                        if item_name.endswith('.pem') and item_name.startswith(('etc/ssl/','usr/local/lib/python')):
                            continue
                        prohibited.append(item_name)
                    # Private/reference paths and actual credentials must not occur in
                    # app code, package/config, or any file. Binary bytes are checked too.
                    stream=layer.extractfile(item)
                    tail=b''
                    longest=max([len(secret) for secret in secret_values]+[1])
                    while block:=stream.read(1024*1024):
                        data=tail+block
                        if any(secret in data for secret in secret_values):
                            secret_hits.append(item_name)
                            break
                        tail=data[-longest:]
    scanned_ok=layers_scanned>0
    result={'scope':'Actual docker save archive: all retained image layers and configuration; exact current credential-byte scan',
            'image':'medical-query-demo:0.3.0','archive_format':('oci' if 'index.json' in names else 'docker-archive'),
            'layers_declared':len(layers),'layers_scanned':layers_scanned,'layer_files_scanned':total_files,
            'project_files':sorted(set(project_files)),
            'prohibited_artifacts':sorted(set(prohibited)),'credential_hits':sorted(set(secret_hits)),
            'archive_sha256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest(),
            'passed':scanned_ok and not prohibited and not secret_hits}
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'layers_scanned':layers_scanned,'layer_files_scanned':total_files,
                      'prohibited_count':len(prohibited),'credential_hits':len(secret_hits)}))
    assert result['passed']


if __name__=='__main__':
    main()
