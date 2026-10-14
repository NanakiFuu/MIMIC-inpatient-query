"""Save fetched official documentation and hashes; no private sources."""
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

SOURCES = {
    'prometheus-config': 'https://prometheus.io/docs/prometheus/latest/configuration/configuration/',
    'prometheus-alerts': 'https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/',
    'prometheus-histograms': 'https://prometheus.io/docs/practices/histograms/',
    'node-exporter': 'https://github.com/prometheus/node_exporter',
    'docker-ubuntu': 'https://docs.docker.com/engine/install/ubuntu/',
    'docker-compose': 'https://docs.docker.com/compose/how-tos/startup-order/',
    'pg-stat-statements': 'https://www.postgresql.org/docs/16/pgstatstatements.html',
    'wsl-config': 'https://learn.microsoft.com/en-us/windows/wsl/wsl-config',
}


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.skip += 1
        if tag in {'p', 'h1', 'h2', 'h3', 'li', 'pre', 'div'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def main():
    output = Path(__file__).resolve().parents[1] / 'evidence/official-docs'
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for key, url in SOURCES.items():
        result = subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                                 '--retry', '2', '--max-time', '30', url], capture_output=True)
        if result.returncode:
            records.append({'url':url, 'fetch_failed':True, 'curl_exit':result.returncode})
            print(key, 'fetch failed', result.returncode, flush=True)
            continue
        raw = result.stdout
        status = 200
        parser = Text()
        parser.feed(raw.decode('utf-8'))
        (output / (key + '.txt')).write_text(''.join(parser.parts), encoding='utf-8')
        records.append({'url': url, 'status': status, 'sha256': hashlib.sha256(raw).hexdigest(),
                        'fetched_at_utc': datetime.now(timezone.utc).isoformat()})
        print(key, status, flush=True)
    (output / 'manifest.json').write_text(json.dumps(records, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
