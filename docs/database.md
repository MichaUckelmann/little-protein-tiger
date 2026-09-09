# Inspecting the corpus database

`data/literature.db` is a plain SQLite file. These are debugging
commands for looking at corpus state directly — moved out of
`README.md` because they are maintainer tooling, not usage.

> The pipeline itself never needs these. `python scripts/doctor.py`
> answers "is my corpus healthy" without any SQL.


Quick inspection commands using Python's sqlite3:

```python
import sqlite3
conn = sqlite3.connect('data/literature.db')

# --- Status summary ---
for r in conn.execute("SELECT download_status, COUNT(*) FROM papers GROUP BY download_status"):
    print(r)

for r in conn.execute("SELECT curation_status, COUNT(*) FROM papers GROUP BY curation_status"):
    print(r)

# --- Total counts ---
conn.execute("SELECT COUNT(*) FROM papers").fetchone()
conn.execute("SELECT COUNT(*) FROM papers WHERE download_status = 'downloaded'").fetchone()
conn.execute("SELECT COUNT(*) FROM papers WHERE curation_status = 'completed'").fetchone()

# --- Downloaded but not yet curated (ready to process) ---
conn.execute("""
    SELECT COUNT(*) FROM papers
    WHERE download_status = 'downloaded' AND curation_status != 'completed'
""").fetchone()

# --- Papers by source ---
for r in conn.execute("SELECT source, COUNT(*) FROM papers GROUP BY source"):
    print(r)

# --- Recent papers ---
for r in conn.execute("""
    SELECT title, journal, year, curation_status
    FROM papers ORDER BY year DESC LIMIT 10
"""):
    print(r)

# --- Browse fingerprints by keyword ---
for r in conn.execute("""
    SELECT title, fingerprint_path FROM papers
    WHERE curation_status = 'completed' AND title LIKE '%SYS1%'
"""):
    print(r)

# --- Curation failures ---
for r in conn.execute("""
    SELECT paper_key, curation_error FROM papers
    WHERE curation_status = 'failed'
"""):
    print(r)
```

Or use the sqlite3 CLI directly:

```bash
sqlite3 data/literature.db "SELECT curation_status, COUNT(*) FROM papers GROUP BY curation_status"
```

---

