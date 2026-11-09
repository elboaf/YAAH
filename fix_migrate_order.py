INPUT = 'backend/db/database.py'
with open(INPUT, 'r', encoding='utf-8') as f:
    lines = f.readlines()

output = []
i = 0
fixed = False

while i < len(lines):
    line = lines[i]
    
    # Fix: add to _migrated BEFORE calling backfill, so search's get_db() returns quickly
    if not fixed and '    await _fts5_backfill(db)' in line:
        output.append('    # Mark migration as complete before backfill so get_db() calls from search return quickly\n')
        output.append('    _migrated.add(str(DB_PATH))\n')
        output.append('    # Run one-time FTS5 backfill (no-op if already done)\n')
        output.append('    await _fts5_backfill(db)\n')
        # Skip the next line which is the original _migrated.add
        i += 1
        while i < len(lines) and '_migrated.add(str(DB_PATH))' in lines[i]:
            i += 1
        fixed = True
        continue
    
    output.append(line)
    i += 1

with open(INPUT, 'w', encoding='utf-8') as f:
    f.writelines(output)

print('Migration order fix applied')