import json, re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
SRC = r".yaah-attachments/steering a run should cause the users ch.md"
lines = open(SRC, encoding='utf-8', errors='replace').read().splitlines()
# parse into events
events = []
i = 0
cur = None
# We'll do simple: identify headers
def kind_of(l):
    if l.startswith('**\U0001f9e4 user**') or 'user**' in l and l.startswith('**'):
        return 'user'
    if l.startswith('**\U0001f916 assistant**'):
        return 'assistant'
    if l.startswith('**\U0001f916 tool call:'):
        return 'call'
    if l.startswith('**\U0001f527 tool:'):
        return 'result'
    return None
for idx,l in enumerate(lines):
    k = kind_of(l.strip())
    if k:
        events.append((idx,k,l.strip()))
print(len(events))
c = collections.Counter(k for _,k,_ in events)
print(c)
# extract tool names
names = collections.Counter()
for idx,k,l in events:
    if k=='call':
        m = re.search(r'tool call: (\w+)', l)
        if m: names[m.group(1)]+=1
    if k=='result':
        m = re.search(r'tool: (\w+)', l)
        if m: names[m.group(1)+'_res']+=1
print(names)
