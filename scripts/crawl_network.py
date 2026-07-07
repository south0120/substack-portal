import json, os, time, re, urllib.request, urllib.error, urllib.parse, ssl, datetime
CTX=ssl.create_default_context(cafile=__import__('certifi').where())
# ROOT = repo root (this file lives in <repo>/scripts/). ローカルでもGH Actionsでも同じに解決。
ROOT=os.environ.get('FYL_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDS=f'{ROOT}/feeds.json'
PROG=os.environ.get('FYL_PROG_NETWORK','/tmp/network/progress.jsonl')
OUT=f'{ROOT}/docs/data/network.json'
UA={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
# GitHub の IP は Substack に弾かれるため、FYL_PROXY_* があれば Worker プロキシ経由で取得する。
PROXY_URL=os.environ.get('FYL_PROXY_URL','').rstrip('/')
PROXY_SECRET=os.environ.get('FYL_PROXY_SECRET','')

def handle_of(w):
    for key in ('feed_url','url'):
        u=w.get(key) or ''
        m=re.match(r'https?://([a-z0-9-]+)\.substack\.com', u)
        if m: return m.group(1)
        m=re.match(r'https?://([a-z0-9.-]+)/', u+'/')
        if m and 'substack.com' not in m.group(1) and '.' in m.group(1): return m.group(1)
    return None

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
            if e.code==429: time.sleep(25+10*i); continue
            return {'__err':e.code}
        except Exception:
            time.sleep(2)
            if i==tries-1: return {'__err':'net'}
    return {'__err':'429max'}

def links_of(handle):
    # 双方向: recommends(/from=推薦してる先) と recommended_by(/to=推薦されてる元=被推薦の実数)
    out=[]; inb=[]
    hp=get(f'https://{handle}.substack.com/api/v1/homepage_data')
    if not isinstance(hp,dict): return {'recommends':out,'recommended_by':inb}
    recs=hp.get('recommendations') or []
    pubid=None
    if recs: pubid=recs[0].get('recommending_publication_id')
    if not pubid:
        for key in ('newPosts','topPosts','pinnedPosts','postExcerpts'):
            arr=hp.get(key)
            if isinstance(arr,list):
                for p in arr:
                    if isinstance(p,dict) and p.get('publication_id'): pubid=p['publication_id']; break
            if pubid: break
    if not pubid:
        # フォールバック: homepage_dataのtop5(推薦先のみ)
        for r in recs:
            pub=r.get('recommendedPublication') or {}
            if pub.get('subdomain'): out.append({'to':pub['subdomain'],'to_name':pub.get('name')})
        return {'recommends':out,'recommended_by':inb}
    frm=get(f'https://{handle}.substack.com/api/v1/recommendations/from/{pubid}')
    if isinstance(frm,list):
        for r in frm:
            pub=r.get('recommendedPublication') or {}
            if pub.get('subdomain'): out.append({'to':pub['subdomain'],'to_name':pub.get('name')})
    to=get(f'https://{handle}.substack.com/api/v1/recommendations/to/{pubid}')
    if isinstance(to,list):
        for r in to:
            pub=r.get('recommendingPublication') or {}
            if pub.get('subdomain'): inb.append({'frm':pub['subdomain'],'frm_name':pub.get('name')})
    return {'recommends':out,'recommended_by':inb}

def main():
    os.makedirs(os.path.dirname(PROG) or '.', exist_ok=True)
    feeds=json.load(open(FEEDS))
    writers = feeds['feeds'] if isinstance(feeds,dict) else feeds
    done=set()
    if os.path.exists(PROG):
        for l in open(PROG):
            try: done.add(json.loads(l)['handle'])
            except: pass
    f=open(PROG,'a'); tot=len(writers); cnt=0
    for w in writers:
        h=handle_of(w)
        if not h: continue
        if h in done: cnt+=1; continue
        lk=links_of(h)
        rec={'handle':h,'name':w.get('name') or h,'categories':w.get('categories') or [],
             'recommends':lk['recommends'],'recommended_by':lk['recommended_by'],
             'inbound_count':len(lk['recommended_by']),'name_by_handle':{}}
        # 推薦元/先の表示名マップ（外部ノードの名前用）
        nm={}
        for x in lk['recommends']:
            if x.get('to_name'): nm[x['to']]=x['to_name']
        for x in lk['recommended_by']:
            if x.get('frm_name'): nm[x['frm']]=x['frm_name']
        rec['name_by_handle']=nm
        f.write(json.dumps(rec,ensure_ascii=False)+'\n'); f.flush()
        cnt+=1
        if cnt%25==0: print(f'{cnt}/{tot}', flush=True)
        time.sleep(0.5)
    f.close()
    # assemble nodes + edges（双方向）
    rows=[json.loads(l) for l in open(PROG) if l.strip()]
    fyl=set(r['handle'] for r in rows)
    extname={}  # handle -> 表示名（外部ノード用）
    for r in rows:
        for k,v in (r.get('name_by_handle') or {}).items(): extname.setdefault(k,v)
    # エッジを双方向ソースから集めて重複排除
    eset={}  # (s,t) -> ext
    for r in rows:
        s=r['handle']
        for x in r['recommends']:
            t=x['to']; eset[(s,t)]= (s not in fyl) or (t not in fyl)
        for x in r['recommended_by']:
            s2=x['frm']; eset[(s2,s)]= (s2 not in fyl) or (s not in fyl)
    edges=[{'s':k[0],'t':k[1],'ext':v} for k,v in eset.items()]
    # 被推薦(indeg)はFYL writerは /to の実数、外部はエッジから集計
    inb_true={r['handle']:r.get('inbound_count',0) for r in rows}
    edge_indeg={}
    for e in edges: edge_indeg[e['t']]=edge_indeg.get(e['t'],0)+1
    nodes=[]; seen=set()
    for r in rows:
        nodes.append({'id':r['handle'],'name':r['name'],'cat':(r['categories'] or ['その他'])[0],
                      'indeg':inb_true.get(r['handle'],0),'ext':False}); seen.add(r['handle'])
    for e in edges:
        for end in (e['s'],e['t']):
            if end not in seen:
                nodes.append({'id':end,'name':extname.get(end,end),'cat':'外部','indeg':edge_indeg.get(end,0),'ext':True}); seen.add(end)
    json.dump({'generated':datetime.datetime.now(datetime.UTC).isoformat(),'nodes':nodes,'edges':edges,
               'stats':{'writers':len(rows),'edges':len(edges),'intra':sum(1 for e in edges if not e['ext']),'ext_nodes':sum(1 for n in nodes if n['ext'])}},
              open(OUT,'w'), ensure_ascii=False)
    print(f"DONE_NET nodes={len(nodes)} edges={len(edges)} intra={sum(1 for e in edges if not e['ext'])} -> {OUT}", flush=True)

main()
