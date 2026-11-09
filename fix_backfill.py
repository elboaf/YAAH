import re

INPUT = 'backend/db/database.py'
with open(INPUT, 'r', encoding='utf-8') as f:
    lines = f.readlines()

output = []
i = 0
fixed_backfill = False
fixed_call = False

while i < len(lines):
    line = lines[i]
    
    # Fix the backfill function to not call get_db() again
    if not fixed_backfill and 'async def _fts5_backfill():' in line:
        output.append('async def _fts5_backfill(db):\n')
        i += 1
        # Skip the "db = await get_db()" line
        while i < len(lines) and not lines[i].strip().startswith('try:'):
            if 'db = await get_db()' in lines[i]:
                i += 1
                continue
            output.append(lines[i])
            i += 1
        # Now at "try:"
        output.append(lines[i])
        i += 1
        # Continue copying until "finally:"
        while i < len(lines) and not lines[i].strip().startswith('finally:'):
            output.append(lines[i])
            i += 1
        # Skip the "finally:" and "await db.close()" lines
        while i < len(lines) and not lines[i].strip().startswith('async def ') and not lines[i].strip().startswith('def '):
            if lines[i].strip() == 'finally:' or lines[i].strip() == 'await db.close()':
                i += 1
                continue
            output.append(lines[i])
            i += 1
        fixed_backfill = True
        continue
    
    # Fix the call to pass db
    if not fixed_call and 'await _fts5_backfill()' in line:
        output.append('    # Run one-time FTS5 backfill (no-op if already done)\n')
        output.append('    await _fts5_backfill(db)\n')
        i += 1
        fixed_call = True
        continue
    
    output.append(line)
    i += 1

with open(INPUT, 'w', encoding='utf-8') as f:
    f.writelines(output)

print("Backfill fix applied")