import json, os, time, re, sys, urllib.request, urllib.error, urllib.parse, ssl, datetime
CERT=__import__('certifi').where()
CTX=ssl.create_default_context(cafile=CERT)
# ROOT = repo root (this file lives in <repo>/scripts/). ローカルでもGH Actionsでも同じに解決する。
ROOT=os.environ.get('FYL_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDS=f'{ROOT}/feeds.json'
PROG=os.environ.get('FYL_PROG_DISCOVERY','/tmp/discovery/progress.jsonl')
OUT=f'{ROOT}/docs/data/discovery.json'
UA={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
# GitHub の IP は Substack に弾かれるため、FYL_PROXY_* があれば Worker プロキシ経由で取得する。
PROXY_URL=os.environ.get('FYL_PROXY_URL','').rstrip('/')
PROXY_SECRET=os.environ.get('FYL_PROXY_SECRET','')

def handle_of(w):
    for key in ('url','feed_url'):
        u=w.get(key) or ''
        m=re.match(r'https?://([a-z0-9-]+)\.substack\.com', u)
        if m: return ('substack', m.group(1), f"https://{m.group(1)}.substack.com")
        m=re.match(r'https?://([a-z0-9.-]+)/', u+'/')
        if m and 'substack.com' not in m.group(1) and '.' in m.group(1):
            return ('custom', m.group(1), f"https://{m.group(1)}")
    return (None,None,None)

def get(url, tries=3):
    for i in range(tries):
        try:
            target=url; headers=dict(UA)
            if PROXY_URL and PROXY_SECRET:
                target=f"{PROXY_URL}/api/proxy?url={urllib.parse.quote(url, safe='')}"
                headers['x-proxy-secret']=PROXY_SECRET
            req=urllib.request.Request(target, headers=headers)
            return json.load(urllib.request.urlopen(req, context=CTX, timeout=25))
        except urllib.error.HTTPError as e:
            if e.code==429:
                time.sleep(25+10*i); continue
            return {'__err':e.code}
        except Exception as e:
            time.sleep(2);
            if i==tries-1: return {'__err':str(e)[:40]}
    return {'__err':'429max'}

def crawl(base):
    arch=get(f'{base}/api/v1/archive?sort=new&limit=12')
    rec=get(f'{base}/api/v1/homepage_data')
    recommendations = rec.get('numRecommendations') if isinstance(rec,dict) and '__err' not in rec else None
    if not isinstance(arch,list):
        return {'err': arch.get('__err') if isinstance(arch,dict) else 'noarchive', 'recommendations':recommendations,
                'avg_reactions':None,'avg_comments':None,'avg_restacks':None,'recent_posts':0,'last_post':None,'posts_per_month':None,'audio_ratio':None}
    posts=arch
    n=len(posts)
    def avg(vals):
        v=[x for x in vals if isinstance(x,(int,float))]
        return round(sum(v)/len(v),2) if v else 0
    reacts=avg([p.get('reaction_count',0) for p in posts])
    comments=avg([(p.get('comment_count',0) or 0)+(p.get('child_comment_count',0) or 0) for p in posts])
    restacks=avg([p.get('restacks',0) for p in posts])
    audio=[1 for p in posts if p.get('type')=='podcast' or p.get('podcast_url')]
    audio_ratio=round(len(audio)/n,2) if n else None
    dates=[]
    for p in posts:
        d=p.get('post_date') or p.get('published_at')
        if d:
            try: dates.append(datetime.datetime.fromisoformat(d.replace('Z','+00:00')))
            except: pass
    last_post=max(dates).date().isoformat() if dates else None
    ppm=None
    if len(dates)>=2:
        span_days=(max(dates)-min(dates)).days or 1
        ppm=round(len(dates)/ (span_days/30.0),2)
    return {'avg_reactions':reacts,'avg_comments':comments,'avg_restacks':restacks,'recommendations':recommendations,
            'recent_posts':n,'last_post':last_post,'posts_per_month':ppm,'audio_ratio':audio_ratio,'err':None}

def main():
    os.makedirs(os.path.dirname(PROG) or '.', exist_ok=True)
    feeds=json.load(open(FEEDS))
    if isinstance(feeds,dict) and isinstance(feeds.get('feeds'),list):
        writers = feeds['feeds']
    elif isinstance(feeds,list):
        writers = feeds
    else:
        writers = [v for v in feeds.values() if isinstance(v,dict)]
    done=set()
    if os.path.exists(PROG):
        for line in open(PROG):
            try: done.add(json.loads(line)['handle'])
            except: pass
    total=len(writers); cnt=0
    f=open(PROG,'a')
    for w in writers:
        name=w.get('name') or w.get('title') or ''
        kind,handle,base=handle_of(w)
        if not handle: 
            continue
        if handle in done:
            cnt+=1; continue
        data=crawl(base)
        rec={'name':name,'handle':handle,'url':base,'categories':w.get('categories') or ([w['category']] if w.get('category') else []), **data}
        f.write(json.dumps(rec,ensure_ascii=False)+'\n'); f.flush()
        cnt+=1
        if cnt%25==0: print(f'{cnt}/{total} done', flush=True)
        time.sleep(0.6)
    f.close()
    # assemble
    rows=[]
    for line in open(PROG):
        try: rows.append(json.loads(line))
        except: pass
    # composite reach score
    for r in rows:
        ar=r.get('avg_reactions') or 0; ac=r.get('avg_comments') or 0; rs=r.get('avg_restacks') or 0; rc=r.get('recommendations') or 0
        r['score']=round(ar + 2*ac + rs + rc, 2)
    rows.sort(key=lambda r: r['score'])
    json.dump({'generated': datetime.datetime.now(datetime.UTC).isoformat().replace('+00:00','Z'),'count':len(rows),'writers':rows},
              open(OUT,'w'), ensure_ascii=False)
    print(f'DONE assembled {len(rows)} -> {OUT}', flush=True)

main()
