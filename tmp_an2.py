import json, re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
SRC = r".yaah-attachments/steering a run should cause the users ch.md"
lines = open(SRC, encoding='utf-8', errors='replace').read().splitlines()
n=len(lines)
# find headers by exact text
hdrs=[]
for idx,l in enumerate(lines):
    s=l.strip()
    if s in ('**\U0001f9e4 user**','**\U0001f916 assistant**') or s.startswith('**\U0001f916 tool call:') or s.startswith('**\U0001f527 tool:'):
        hdrs.append((idx,s))
def body(a,b): return "\n".join(lines[a+1:b]).strip()
# interstitial assistant texts (before a call header)
inter=[]
cmds=[]
for j,(idx,s) in enumerate(hdrs):
    b = hdrs[j+1][0] if j+1<len(hdrs) else n
    t = body(idx,b)
    if s=='**\U0001f916 assistant**':
        if t: inter.append(t)
    if s.startswith('**\U0001f916 tool call: bash'):
        cmds.append(t)
print("assistant with text:", len(inter))
for t in inter[:40]:
    print('---'); print(t[:400])
print('======COMMANDS')
for c in cmds[:40]:
    print('---'); print(c[:300])
