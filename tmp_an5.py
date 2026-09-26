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
cmds=[]
for j,(idx,s) in enumerate(hdrs):
    if s.startswith('**\U0001f916 tool call:'):
        name = s[len('**\U0001f916 tool call:'):].strip(' *')
        b = hdrs[j+1][0] if j+1<len(hdrs) else n
        t = body(idx,b)
        if name=='bash':
            # find 'command' key line in json block
            m = re.search(r'^\s*"(?:command|Command)"\s*:\s*"(.*)$', t, re.M)
            if m:
                raw = m.group(1).rstrip()
                try: cmd = json.loads(raw)  # raw is like "..." possibly with trailing ,
                except Exception: cmd = raw
            else: cmd = t[:200]
        else:
            continue
        cmds.append((idx, cmd))
def norm(c): return re.sub(r'\s+',' ',c).strip()
seen=collections.Counter(norm(c) for _,c in cmds)
print("unique:", len(seen), "total:", len(cmds))
for k,v in seen.most_common(30): print(v,'|',k[:200])
