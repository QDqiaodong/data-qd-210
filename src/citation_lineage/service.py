from .checks import fields,array,ident

def process(data):
 fields(data,('documents','citations'));docs={}
 for d in array(data['documents'],1,1000):
  fields(d,('id','date','title'));k=ident(d['id']);
  if k in docs:raise ValueError('duplicate document')
  docs[k]={'date':d['date'],'title':d['title']}
 edges=[]
 for c in array(data['citations'],0,5000):
  fields(c,('from','to','page'));a=ident(c['from']);b=ident(c['to']);
  if a not in docs or b not in docs or a==b:raise ValueError('invalid citation')
  edges.append((a,b,c['page']))
 adj={k:[] for k in docs}
 for a,b,p in edges:adj[a].append((b,p))
 for k in adj:adj[k].sort()
 cycles=[];state={}
 def dfs(k,path):
  state[k]=1
  for b,p in adj[k]:
   if state.get(b)==1:cycles.append(path+[b])
   elif state.get(b,0)==0:dfs(b,path+[b])
  state[k]=2
 for k in docs:
  if not state.get(k):dfs(k,[k])
 depth={k:0 for k in docs}
 for _ in docs:
  for a,outs in adj.items():
   for b,_ in outs:depth[b]=max(depth[b],depth[a]+1)
 roots=sorted(k for k in docs if not any(b==k for a,b,_ in edges))
 edges.sort(key=lambda x:(x[0],x[1],x[2]))
 return {'documents':docs,'citations':[{'from':a,'to':b,'page':p} for a,b,p in edges],'roots':roots,'cycles':cycles,'summary':{'documents':len(docs),'citations':len(edges),'roots':len(roots),'cycles':len(cycles),'max_depth':max(depth.values(),default=0)}}
