"""Render existing measured JSON series. No simulation or fabricated points."""
import json
from pathlib import Path
import math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
DIRECTORY=ROOT/'evidence/p0/monitoring'


def points(series,start):
    values=[]
    for item in series.get('result',[]):
        for stamp,value in item.get('values',[]):
            value=float(value)
            if math.isfinite(value):
                values.append((float(stamp)-start,value))
    return sorted(values)


def main():
    outage=json.loads((DIRECTORY/'outage.json').read_text())
    start=outage['range']['start']
    fig,axes=plt.subplots(3,1,figsize=(11,8),sharex=True,layout='constrained')
    for key,label in [('database_ready','Database ready'),('app_scrape_up','Application scrape up')]:
        values=points(outage['series'][key],start)
        axes[0].step([p[0] for p in values],[p[1] for p in values],where='post',label=label)
    axes[0].set_ylabel('Probe status')
    axes[0].set_ylim(-.1,1.2)
    axes[0].legend()
    values=points(outage['series']['error_ratio'],start)
    axes[1].plot([p[0] for p in values],[p[1]*100 for p in values],label='Summary 5xx rate over 1 minute')
    axes[1].axhline(5,color='red',linestyle='--',label='5% alert threshold')
    axes[1].set_ylabel('Error ratio (%)')
    axes[1].legend()
    for item in outage['series']['alerts'].get('result',[]):
        values=[(float(t)-start,float(v)) for t,v in item['values']]
        axes[2].scatter([p[0] for p in values],[item['metric']['alertname']]*len(values),s=12,label=item['metric']['alertname'])
    axes[2].set_ylabel('Firing alert samples')
    axes[2].set_xlabel('Seconds since experiment start (actual observations)')
    for event in outage['events']:
        if event['event'] in {'database_stopped','query_recovered'}:
            for ax in axes:
                ax.axvline(event['utc_epoch']-start,color='gray',linestyle=':')
    fig.suptitle('Actual PostgreSQL outage/recovery | one WSL2 host | generated test traffic')
    fig.savefig(DIRECTORY/'outage.png',dpi=160)
    plt.close(fig)
    load=json.loads((DIRECTORY/'load.json').read_text())
    fig,axes=plt.subplots(2,2,figsize=(11,7),layout='constrained')
    for run in load['runs']:
        start=run['start_utc_epoch']
        for ax,key,label,multiplier in [(axes[0,0],'cpu_busy_ratio','WSL CPU busy (%)',100),
                                         (axes[0,1],'memory_available_bytes','WSL available memory (GiB)',1/2**30),
                                         (axes[1,0],'server_p95_seconds','Server histogram p95 (ms)',1000)]:
            values=points(run['series'][key],start)
            ax.plot([p[0] for p in values],[p[1]*multiplier for p in values],label=f"concurrency {run['concurrency']}")
            ax.set_ylabel(label)
            ax.set_xlabel('Seconds within stage')
            ax.legend()
    axes[1,1].bar([str(r['concurrency']) for r in load['runs']],[r['requests_per_second'] for r in load['runs']])
    axes[1,1].set_xlabel('Closed-loop concurrency')
    axes[1,1].set_ylabel('Observed requests/second')
    fig.suptitle('Measured fixed API load | same-host client | no AI calls | no capacity-limit claim')
    fig.savefig(DIRECTORY/'load.png',dpi=160)
    plt.close(fig)
    print('Rendered existing measured series to outage.png and load.png')


if __name__=='__main__':
    main()
