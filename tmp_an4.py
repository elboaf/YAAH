import sys, io, collections, re, json
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
calls=[]
for j,(idx,s) in enumerate(hdrs):
    if s.startswith('**\U0001f916 tool call:'):
        name = s[len('**\U0001f916 tool call:'):].strip(' *')
        b = hdrs[j+1][0] if j+1<len(hdrs) else n
        calls.append((idx, name, body(idx,b)))
cmds=[]
for idx,name,t in calls:
    if name=='bash':
        m = re.search(r'[Cc]ommand" ?:[ ]*"((?:[^"\]|\.)*)"', t)
        cmd = m.group(1) if m else t[:300]
        try:
            cmd = json.loads('"'+cmd+'"')
        except Exception:
            pass
        cmds.append((idx, cmd))
def norm(c): return re.sub(r'\s+',' ',c).strip()
seen=collections.Counter(norm(c) for _,c in cmds)
print("unique:", len(seen), "total:", len(cmds))
for k,v in seen.most_common(25): print(v, '|', k[:180])
