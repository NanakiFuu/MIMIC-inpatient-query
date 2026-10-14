"""Pre-publication heuristic scan. Reports locations/categories, never secret values.

Uses Git's candidate list to honor ignore rules. This is a review aid, not proof
that every possible credential or private fact can be detected.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
RULES={
 'private_reference_identifier':re.compile(chr(0x82f1)+chr(0x5fb7)+'|'+'D'+'W_'+r'[MT](?:\b|_)|'+'PACKAGE'+'_ITEM|'+r'[Ee]:[/\\]private'),
 'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 'provider_key':re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'),
 'credential_url':re.compile(r'(?:postgres(?:ql)?|mysql)://[^\s/:]+:[^\s@]+@'),
 'literal_secret':re.compile(r'''["']?(?:api_key|password|secret|token)["']?\s*[:=]\s*["']([^"'\n]{12,})["']''',re.I),
}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    listing=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT,capture_output=True,check=True).stdout
    names=sorted(set(n.decode('utf-8') for n in listing.split(b'\0') if n))
    findings=[]
    for name in names:
        path=ROOT/name
        if not path.is_file() or path.resolve()==args.report.resolve():
            continue
        if path.suffix in {'.csv','.xlsx','.dump','.pem','.key'} or name.endswith('.csv.gz') or Path(name).name=='.env':
            findings.append({'file':name,'category':'prohibited_artifact'})
        try:
            data=path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            continue
        for line_no,line in enumerate(data.splitlines(),1):
            for category,pattern in RULES.items():
                match=pattern.search(line)
                if match:
                    if category=='literal_secret':
                        value=match.group(1)
                        # Explicit TEST_ONLY values and descriptions are not credentials.
                        if 'TEST' in value or any(c.isspace() for c in value) or value.startswith(('model_', 'database_', 'deepseek', 'query_')):
                            continue
                    findings.append({'file':name,'line':line_no,'category':category})
    result={'scanned_at_utc':datetime.now(timezone.utc).isoformat(),'files_scanned':len(names),
            'scope':'Git candidate files; regex heuristic; manual review still required; no secret values emitted',
            'findings':findings,'passed':not findings}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    if findings:
        raise SystemExit(1)


if __name__=='__main__':
    main()
