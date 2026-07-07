import json, sys, os
import networkx as nx
ROOT=os.environ.get('FYL_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET=f'{ROOT}/docs/data/network.json'
d=json.load(open(NET))
nodes={n['id']:n for n in d['nodes']}
fyl=set(n['id'] for n in d['nodes'] if not n['ext'])
# intra 有向グラフ
DG=nx.DiGraph(); UG=nx.Graph()
for nid in fyl: DG.add_node(nid); UG.add_node(nid)
intra=[(e['s'],e['t']) for e in d['edges'] if e['s'] in fyl and e['t'] in fyl]
for s,t in intra:
    DG.add_edge(s,t); UG.add_edge(s,t)
print(f'FYLノード {len(fyl)} / intra有向エッジ {DG.number_of_edges()}')

# PageRank（有向）
pr=nx.pagerank(DG, alpha=0.85)
# 媒介中心性（無向・正規化）
bc=nx.betweenness_centrality(UG, normalized=True)
# Louvainコミュニティ（無向）
comms=nx.community.louvain_communities(UG, seed=42)
comm_of={}
for ci,c in enumerate(sorted(comms,key=len,reverse=True)):
    for nid in c: comm_of[nid]=ci
# 相互/片想い
edgeset=set(intra)
mutual_pairs=set(); oneway=0
for s,t in intra:
    if (t,s) in edgeset:
        mutual_pairs.add(tuple(sorted((s,t))))
    else:
        oneway+=1
# リンク予測（Adamic-Adar、共通隣接2以上の非エッジペアに限定）
from itertools import combinations
cand=set()
for n in UG.nodes():
    nb=list(UG.neighbors(n))
    for a,b in combinations(nb,2):
        if not UG.has_edge(a,b) and a!=b: cand.add(tuple(sorted((a,b))))
aa=sorted(nx.adamic_adar_index(UG, cand), key=lambda x:-x[2])[:30]

# enrich nodes
for n in d['nodes']:
    if n['id'] in fyl:
        n['pagerank']=round(pr.get(n['id'],0),5)
        n['betweenness']=round(bc.get(n['id'],0),5)
        n['community']=comm_of.get(n['id'],-1)
def nm(h): return nodes.get(h,{}).get('name',h)
top_pr=sorted(fyl,key=lambda h:-pr.get(h,0))[:12]
top_bc=sorted([h for h in fyl if bc.get(h,0)>0],key=lambda h:-bc.get(h,0))[:12]
d['analysis']={
  'communities':[{'id':ci,'size':len(c),'top':[nm(h) for h in sorted(c,key=lambda h:-pr.get(h,0))[:3]]} for ci,c in enumerate(sorted(comms,key=len,reverse=True)) if len(c)>=4],
  'top_pagerank':[{'name':nm(h),'pagerank':round(pr[h],4),'indeg':nodes[h]['indeg']} for h in top_pr],
  'top_bridges':[{'name':nm(h),'betweenness':round(bc[h],4)} for h in top_bc],
  'mutual_count':len(mutual_pairs),'oneway_count':oneway,
  'mutual_examples':[[nm(a),nm(b)] for a,b in list(mutual_pairs)[:10]],
  'link_predictions':[{'a':nm(a),'b':nm(b),'score':round(s,2)} for a,b,s in aa[:15]],
}
json.dump(d, open(NET,'w'), ensure_ascii=False)
A=d['analysis']
print(f"コミュニティ数(4+): {len(A['communities'])} | 相互推薦ペア {A['mutual_count']} / 片想い {A['oneway_count']}")
print('--- PageRank影響力 Top5 ---')
for x in A['top_pagerank'][:5]: print(f"  PR{x['pagerank']} 被推薦{x['indeg']} {x['name'][:24]}")
print('--- 橋渡し役(媒介中心性) Top5 ---')
for x in A['top_bridges'][:5]: print(f"  BC{x['betweenness']} {x['name'][:24]}")
print('--- コミュニティ Top4 ---')
for c in A['communities'][:4]: print(f"  C{c['id']} ({c['size']}人): {' / '.join(c['top'])}")
print('--- おすすめ推薦ペア Top5 ---')
for x in A['link_predictions'][:5]: print(f"  {x['score']} {x['a'][:18]} ⇔ {x['b'][:18]}")
