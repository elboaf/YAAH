import sys, io, collections, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
SRC = r".yaah-attachments/steering a run should cause the users ch.md"
lines = open(SRC, encoding='utf-8', errors='replace').read().splitlines()
n=len(lines)
hdrs=[]
for idx,l in enumerate(lines):
    s=l.strip()
    if s in ('**\U0001f9e4 user**','**\U0001f916 assistant**') or s.startswith('**\U0001f916 tool call:') or s.startswith('**\U0001f527 tool:'):
        hdrs.append((idx,s))
def body(a,b): return "\n".join(lines[a+1:b]).strip()
# match each call header to next result header
calls=[]
for j,(idx,s) in enumerate(hdrs):
    if s.startswith('**\U0001f916 tool call:'):
        name = s[len('**\U0001f916 tool call:'):].strip(' *')
        b = hdrs[j+1][0] if j+1<len(hdrs) else n
        calls.append((idx, name, body(idx,b)))
print(len(calls), collections.Counter(c[1] for c in calls))
# extract command text of bash calls
cmds=[]
for idx,name,t in calls:
    if name=='bash':
        m = re.search(r'"command"\s*:\s*"((?:[^"\]|\.)*)"', t)
        cmd = m.group(1) if m else t[:200]
        try: cmd = json.loads('"'+cmd+'"') if False else cmd.encode().decode('unicode_escape')
        except Exception: pass
        cmds.append((idx, cmd))
# normalize & find repeats
import hashlib
def norm(c): return re.sub(r'\s+',' ',c).strip()
seen=collections.Counter(norm(c) for _,c in cmds)
print("unique:", len(seen), "total:", len(cmds))
for k,v in seen.most_common(15): print(v, '|', k[:150])
