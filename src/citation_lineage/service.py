from collections import deque
from .checks import fields,array,ident

def text(value):
 if not isinstance(value,str) or not 1<=len(value)<=256 or any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in value):raise ValueError('bounded text required')
 return value

def process(data):
 fields(data,('documents','citations'));docs={}
 for d in array(data['documents'],1,1000):
  fields(d,('id','date','title'));k=ident(d['id'])
  if k in docs:raise ValueError('duplicate document')
  docs[k]={'date':text(d['date']),'title':text(d['title'])}
 edges=[];seen=set();adj={k:set() for k in docs};reverse={k:set() for k in docs}
 for c in array(data['citations'],0,5000):
  fields(c,('from','to','page'));a=ident(c['from']);b=ident(c['to']);page=text(c['page'])
  if a not in docs or b not in docs or a==b:raise ValueError('invalid citation')
  edge=(a,b,page)
  if edge in seen:raise ValueError('duplicate citation locator')
  seen.add(edge);edges.append(edge);adj[a].add(b);reverse[b].add(a)
 visited=set();finish=[]
 for start in sorted(docs):
  if start in visited:continue
  visited.add(start);stack=[(start,iter(sorted(adj[start])))]
  while stack:
   node,it=stack[-1];child=next(it,None)
   if child is None:finish.append(node);stack.pop()
   elif child not in visited:visited.add(child);stack.append((child,iter(sorted(adj[child]))))
 visited=set();cycles=[]
 for start in reversed(finish):
  if start in visited:continue
  members=[];pending=[start];visited.add(start)
  while pending:
   node=pending.pop();members.append(node)
   for child in reverse[node]:
    if child not in visited:visited.add(child);pending.append(child)
  if len(members)>1:cycles.append(sorted(members))
 cycles.sort();roots=sorted(k for k in docs if not reverse[k]);depth={k:0 for k in docs}
 indegree={k:len(v) for k,v in reverse.items()};pending=deque(roots)
 while pending:
  node=pending.popleft()
  for child in sorted(adj[node]):
   depth[child]=max(depth[child],depth[node]+1);indegree[child]-=1
   if indegree[child]==0:pending.append(child)
 ancestry={}
 for source in sorted(docs):
  found=set();pending=list(adj[source])
  while pending:
   node=pending.pop()
   if node in found:continue
   found.add(node);pending.extend(adj[node]-found)
  found.discard(source);ancestry[source]=sorted(found)
 return {'documents':dict(sorted(docs.items())),'citations':[{'from':a,'to':b,'page':p} for a,b,p in sorted(edges)],'roots':roots,'cycles':cycles,'cited_ancestors':ancestry,'summary':{'documents':len(docs),'citations':len(edges),'roots':len(roots),'cycles':len(cycles),'max_depth':None if cycles else max(depth.values(),default=0)}}
