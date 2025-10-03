#!/usr/bin/env python3
"""
mysql_to_pg_schema_cleaner.py

Place all your MySQL per-table .sql files in INPUT_DIR.
It will produce OUTPUT_FILE which is a Postgres-ready schema (structure-only).
"""

import os
import re

INPUT_DIR = "sql"      # folder containing your .sql files
OUTPUT_FILE = "schema_postgres.sql"
TARGET_SCHEMA = "myapp"     # change schema name here

# -------------------------
# Helper text-cleaning
# -------------------------
def remove_mysql_noise(sql):
    # Remove /*! ... */ versioned comments and other /* ... */ blocks from phpMyAdmin
    sql = re.sub(r"/\*![\s\S]*?\*/", "", sql)
    sql = re.sub(r"/\*[\s\S]*?\*/", "", sql)

    # Remove single-line mysql comments that begin with -- or #
    sql = re.sub(r"(?m)^\s*--.*\n?", "", sql)
    sql = re.sub(r"(?m)^\s*#.*\n?", "", sql)
    
    # Remove inline comments (-- comment at end of line)
    sql = re.sub(r"--[^\r\n]*", "", sql)

    # Remove SET statements (sql_mode, time_zone etc.) and START/COMMIT/LOCK/UNLOCK
    sql = re.sub(r"(?im)^\s*SET\s+[^;]+;\s*", "", sql)
    sql = re.sub(r"(?im)^\s*START\s+TRANSACTION\s*;\s*", "", sql)
    sql = re.sub(r"(?im)^\s*COMMIT\s*;\s*", "", sql)
    sql = re.sub(r"(?im)^\s*LOCK TABLES\b.*?;\s*", "", sql, flags=re.DOTALL)
    sql = re.sub(r"(?im)^\s*UNLOCK TABLES\s*;\s*", "", sql)

    # Remove ENGINE / AUTO_INCREMENT=<n> / DEFAULT CHARSET / COLLATE at end of CREATE TABLE
    sql = re.sub(r"(?im)ENGINE\s*=\s*\w+\s*", "", sql)
    sql = re.sub(r"(?im)AUTO_INCREMENT\s*=\s*\d+\s*", "", sql)
    sql = re.sub(r"(?im)DEFAULT\s+CHARSET\s*=\s*\w+\s*", "", sql)
    sql = re.sub(r"(?im)CHARSET\s*=\s*\w+\s*", "", sql)
    sql = re.sub(r"(?im)COLLATE\s*=\s*[\w\-_]+\s*", "", sql)

    # Remove repeated phpMyAdmin headers or other garbage lines like /*!40101 ... */
    sql = re.sub(r"(?m)^\s*-- phpMyAdmin.*\n?", "", sql)
    sql = re.sub(r"(?m)^\s*-- Host:.*\n?", "", sql)
    sql = re.sub(r"(?m)^\s*-- Generation Time:.*\n?", "", sql)
    sql = re.sub(r"(?m)^\s*-- Server version:.*\n?", "", sql)

    return sql

# -------------------------
# Utility: find create-table blocks (balanced parentheses)
# -------------------------
def find_create_table_blocks(sql):
    blocks = []
    pos = 0
    low = sql.lower()
    while True:
        m = re.search(r"create\s+table", low[pos:], re.IGNORECASE)
        if not m:
            break
        start = pos + m.start()
        # find the first '(' after start
        paren_idx = sql.find("(", start)
        if paren_idx == -1:
            pos = start + 1
            continue
        # balance parentheses
        i = paren_idx
        depth = 0
        end_idx = -1
        while i < len(sql):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    # find semicolon after this )
                    # skip whitespace and comments until semicolon
                    j = i + 1
                    while j < len(sql) and sql[j].isspace():
                        j += 1
                    if j < len(sql) and sql[j] == ";":
                        end_idx = j
                    else:
                        # try to find next semicolon a bit further (some dumps omit immediate ;)
                        sc = sql.find(";", i+1)
                        end_idx = sc if sc != -1 else i
                    break
            i += 1
        if end_idx == -1:
            # malformed; take until next semicolon to avoid swallowing everything
            sc = sql.find(";", paren_idx)
            end_idx = sc if sc != -1 else len(sql)-1

        block_text = sql[start:end_idx+1]
        blocks.append((start, end_idx+1, block_text))
        pos = end_idx+1
    return blocks

# -------------------------
# Extract ALTER TABLE statements
# -------------------------
def find_alter_statements(sql):
    alters = []
    for m in re.finditer(r"alter\s+table\b", sql, flags=re.IGNORECASE):
        start = m.start()
        sc = sql.find(";", start)
        if sc == -1:
            sc = len(sql)-1
        block = sql[start:sc+1]
        alters.append(block)
    return alters

# -------------------------
# Helpers to normalize identifier names
# -------------------------
def clean_identifier(raw):
    # remove backticks, quotes, surrounding whitespace
    raw = raw.strip()
    raw = raw.strip("`\"")
    # if name is db.table, take last part
    if "." in raw:
        parts = [p.strip("`\" ") for p in raw.split(".")]
        return parts[-1]
    return raw

def quote_ident(name):
    # simple quoting (no escaping of double-quotes inside name)
    return f'"{name}"'

# -------------------------
# Main conversion logic
# -------------------------
def convert_create_block(block_text, table_autoinc_map):
    # Extract raw table name from header
    # find header up to first '('
    header_match = re.search(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(.+?)\(", block_text, flags=re.IGNORECASE|re.DOTALL)
    if not header_match:
        return None, [], []
    raw_name = header_match.group(2).strip()
    raw_name = raw_name.rstrip()
    # strip trailing backtick/quote/spacing
    raw_name = raw_name.strip()
    raw_name = raw_name.split()[-1] if raw_name.split() else raw_name
    table_name = clean_identifier(raw_name)

    # find inner body from first '(' to matching ')'
    open_idx = block_text.find("(")
    # balance from open_idx
    i = open_idx
    depth = 0
    end_idx = None
    while i < len(block_text):
        if block_text[i] == "(":
            depth += 1
        elif block_text[i] == ")":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
        i += 1
    if end_idx is None:
        raise RuntimeError(f"Couldn't parse CREATE TABLE block for {table_name}")

    body = block_text[open_idx+1:end_idx]

    # split top-level comma separated lines (avoid commas inside parentheses)
    parts = []
    current = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(ch)
    # last part
    last = "".join(current).strip()
    if last:
        parts.append(last)

    column_lines = []
    inline_index_defs = []
    inline_constraints = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        # detect column definition (starts with backtick or quote or alphanumeric name)
        # but exclude MySQL keywords like INDEX, KEY, UNIQUE, FOREIGN, CONSTRAINT, PRIMARY
        if re.match(r"^(INDEX|KEY|UNIQUE|FOREIGN|CONSTRAINT|PRIMARY)\s+", p, flags=re.IGNORECASE):
            col_match = None
        else:
            # Match both quoted and unquoted column names
            col_match = re.match(r"^(`?\"?)(\w+)\1?\s+(.*)$", p, flags=re.DOTALL)
            if not col_match:
                # Try without quotes for unquoted column names
                col_match = re.match(r"^(\w+)\s+(.*)$", p, flags=re.DOTALL)
        if col_match:
            colname = col_match.group(2)
            rest = col_match.group(3).strip()
            orig_rest = rest
            

            # Handle inline foreign key references (PostgreSQL style)
            if "REFERENCES" in rest.upper():
                # Extract foreign key details for later ALTER TABLE statement
                fk_match = re.search(r"REFERENCES\s+(\w+)\s*\(([^)]+)\)\s*(.*)", rest, flags=re.IGNORECASE)
                if fk_match:
                    ref_table = fk_match.group(1)
                    ref_cols = fk_match.group(2)
                    ref_options = fk_match.group(3).strip()
                    
                    # Remove the REFERENCES part from the column definition
                    rest = re.sub(r"\s+REFERENCES\s+\w+\s*\([^)]+\)\s*.*$", "", rest, flags=re.IGNORECASE)
                    
                    # Create ALTER TABLE statement for foreign key
                    fk_constraint = f'ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ADD FOREIGN KEY ({quote_ident(colname)}) REFERENCES {quote_ident(TARGET_SCHEMA)}.{quote_ident(ref_table)} ({ref_cols}) {ref_options};'
                    inline_constraints.append(fk_constraint)

            # convert types
            # tinyint(1) -> boolean
            rest = re.sub(r"\btinyint\s*\(\s*1\s*\)", "BOOLEAN", rest, flags=re.IGNORECASE)
            
            # Fix specific data type issues
            if table_name == "assigned_rooms" and colname == "user_id":
                # Change varchar(10) to integer to match users.id
                rest = re.sub(r"varchar\s*\(\s*\d+\s*\)", "INTEGER", rest, flags=re.IGNORECASE)

            # int(X) -> integer, bigint(X) -> bigint
            rest = re.sub(r"\bint\s*\(\s*\d+\s*\)", "INTEGER", rest, flags=re.IGNORECASE)
            rest = re.sub(r"\bbigint\s*\(\s*\d+\s*\)", "BIGINT", rest, flags=re.IGNORECASE)

            # decimal(a,b) -> NUMERIC(a,b)
            rest = re.sub(r"\bdecimal\s*\(", "NUMERIC(", rest, flags=re.IGNORECASE)

            # double -> double precision
            rest = re.sub(r"\bdouble\b", "double precision", rest, flags=re.IGNORECASE)

            # datetime -> timestamp
            rest = re.sub(r"\bdatetime\b", "TIMESTAMP", rest, flags=re.IGNORECASE)

            # CURRENT_TIMESTAMP() -> CURRENT_TIMESTAMP
            rest = re.sub(r"current_timestamp\s*\(\s*\)", "CURRENT_TIMESTAMP", rest, flags=re.IGNORECASE)

            # ON UPDATE CURRENT_TIMESTAMP -> remove (Postgres doesn't support this inline)
            rest = re.sub(r"ON\s+UPDATE\s+CURRENT_TIMESTAMP", "", rest, flags=re.IGNORECASE)

            # enum('a','b') -> varchar with CHECK
            enum_m = re.search(r"\benum\s*\((.*?)\)", rest, flags=re.IGNORECASE)
            enum_check = None
            if enum_m:
                enum_vals = enum_m.group(1)
                # keep values as-is for check
                rest = re.sub(r"\benum\s*\(.*?\)", "VARCHAR(191)", rest, flags=re.IGNORECASE)
                enum_check = (colname, enum_vals)

            # DEFAULT '1'/'0' for boolean -> DEFAULT TRUE/FALSE (only for boolean columns)
            if re.search(r"\bBOOLEAN\b", rest, flags=re.IGNORECASE):
                rest = re.sub(r"DEFAULT\s+'1'", "DEFAULT TRUE", rest, flags=re.IGNORECASE)
                rest = re.sub(r"DEFAULT\s+'0'", "DEFAULT FALSE", rest, flags=re.IGNORECASE)
                # Also handle numeric defaults for boolean columns
                rest = re.sub(r"DEFAULT\s+1\b", "DEFAULT TRUE", rest, flags=re.IGNORECASE)
                rest = re.sub(r"DEFAULT\s+0\b", "DEFAULT FALSE", rest, flags=re.IGNORECASE)

            # remove unsigned
            rest = re.sub(r"\bunsigned\b", "", rest, flags=re.IGNORECASE)

            # Remove CHARACTER SET/ COLLATE in columns
            rest = re.sub(r"CHARACTER SET\s+\w+", "", rest, flags=re.IGNORECASE)
            rest = re.sub(r"COLLATE\s+\w+", "", rest, flags=re.IGNORECASE)

            # handle AUTO_INCREMENT: prefer converting column type to SERIAL/BIGSERIAL
            if re.search(r"\bAUTO_INCREMENT\b", orig_rest, flags=re.IGNORECASE) or (table_name in table_autoinc_map and colname in table_autoinc_map[table_name]):
                # decide serial type based on original type hint
                if re.search(r"\bBIGINT\b", rest, flags=re.IGNORECASE):
                    serial_type = "BIGSERIAL"
                else:
                    serial_type = "SERIAL"
                # preserve NOT NULL if present
                notnull = " NOT NULL" if re.search(r"\bNOT\s+NULL\b", orig_rest, flags=re.IGNORECASE) else ""
                # Check if this column also has PRIMARY KEY
                if re.search(r"\bPRIMARY\s+KEY\b", orig_rest, flags=re.IGNORECASE):
                    primary_key = " PRIMARY KEY"
                else:
                    primary_key = ""
                # SERIAL implies a default; don't carry AUTO_INCREMENT literal or NOT NULL duplication
                col_def = f'{quote_ident(colname)} {serial_type}{notnull}{primary_key}'
            else:
                # otherwise keep the converted rest but tidy up NOT NULL and DEFAULT placements
                # Remove multiple spaces
                rest = re.sub(r"\s+", " ", rest).strip()
                col_def = f'{quote_ident(colname)} {rest}'

            column_lines.append(col_def)
            if enum_check:
                # add simple CHECK (col IN (...))
                v = enum_check[1]
                # ensure values are quoted
                column_lines.append(f'-- CHECK for enum values on {colname} will be created below')
                inline_constraints.append(f'ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ADD CHECK ({quote_ident(colname)} IN ({v}));')
        else:
            # likely index, constraint or primary key line
            lp = p.strip().rstrip(",")
            # PRIMARY KEY inline -> keep inside create
            if re.match(r"PRIMARY\s+KEY", lp, flags=re.IGNORECASE):
                # normalize quoting of column names inside
                lp2 = re.sub(r"`", "", lp)
                lp2 = re.sub(r'\"', '', lp2)
                column_lines.append(lp2)
            # KEY / UNIQUE KEY / INDEX: capture for post-create index creation
            elif re.match(r"(UNIQUE\s+KEY|UNIQUE\s+INDEX|KEY|INDEX)\s+`?\"?(\w+)`?\"?\s*\((.+)\)", lp, flags=re.IGNORECASE):
                m_idx = re.match(r"(UNIQUE\s+KEY|UNIQUE\s+INDEX|KEY|INDEX)\s+`?\"?(\w+)`?\"?\s*\((.+)\)", lp, flags=re.IGNORECASE)
                kind = m_idx.group(1).upper()
                idx_name = m_idx.group(2)
                cols = m_idx.group(3)
                cols = re.sub(r"[`\"]", "", cols)
                unique = "UNIQUE " if "UNIQUE" in kind else ""
                inline_index = f'CREATE {unique}INDEX IF NOT EXISTS {quote_ident(idx_name)} ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ({cols});'
                inline_index_defs.append(inline_index)
                # Don't add to column_lines - this will be handled separately
            # Handle UNIQUE constraint without KEY/INDEX keyword (e.g., UNIQUE(col1, col2))
            elif re.match(r"UNIQUE\s*\((.+)\)", lp, flags=re.IGNORECASE):
                unique_match = re.match(r"UNIQUE\s*\((.+)\)", lp, flags=re.IGNORECASE)
                if unique_match:
                    cols = unique_match.group(1)
                    cols = re.sub(r"[`\"]", "", cols)
                    # Generate a unique index name
                    idx_name = f"unique_{table_name}_{cols.replace(',', '_').replace(' ', '').replace('(', '').replace(')', '')}"
                    inline_index = f'CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident(idx_name)} ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ({cols});'
                    inline_index_defs.append(inline_index)
                # Don't add to column_lines - this will be handled separately
            # CONSTRAINT (FOREIGN KEY) inline -> capture for post-create constraint creation
            elif re.search(r"FOREIGN\s+KEY", lp, flags=re.IGNORECASE):
                # Extract foreign key details for later ALTER TABLE statement
                fk_match = re.search(r"FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+(\w+)\s*\(([^)]+)\)\s*(.*)", lp, flags=re.IGNORECASE)
                if fk_match:
                    fk_cols = fk_match.group(1).replace("`", "").replace('"', "")
                    ref_table = fk_match.group(2)
                    ref_cols = fk_match.group(3).replace("`", "").replace('"', "")
                    ref_options = fk_match.group(4).strip()
                    
                    # Fix known table reference issues
                    if ref_table == "registrations":
                        ref_table = "users"
                    
                    # Fix data type mismatches
                    if table_name == "assigned_rooms" and colname == "user_id" and ref_table == "users":
                        # Change user_id from varchar to integer to match users.id
                        # This will be handled by updating the column definition
                        pass
                    
                    # Create ALTER TABLE statement for foreign key
                    fk_constraint = f'ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ADD FOREIGN KEY ({fk_cols}) REFERENCES {quote_ident(TARGET_SCHEMA)}.{quote_ident(ref_table)} ({ref_cols}) {ref_options};'
                    inline_constraints.append(fk_constraint)
                # Don't add to column_lines - this will be handled separately
            else:
                # fallback: put as comment so it doesn't break
                column_lines.append(f'-- SKIPPED: {lp}')
    # Deduplicate inline_index_defs
    inline_index_defs = list(dict.fromkeys(inline_index_defs))

    # Build CREATE TABLE statement
    create_lines = []
    create_lines.append(f'CREATE TABLE IF NOT EXISTS {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} (')
    # join column lines with comma
    for idx, cl in enumerate(column_lines):
        comma = "," if idx != len(column_lines)-1 else ""
        create_lines.append("    " + cl + comma)
    create_lines.append(");")

    # return created SQL, indexes and inline constraints
    return "\n".join(create_lines), inline_index_defs, inline_constraints

def process_alters(alter_blocks, table_autoinc_map):
    post_statements = []
    for a in alter_blocks:
        # normalize whitespace
        a_clean = re.sub(r"\s+", " ", a).strip().rstrip(";")
        # capture table name
        m = re.match(r"ALTER\s+TABLE\s+`?\"?([^`\" ]+)`?\"?\s+(.*)$", a_clean, flags=re.IGNORECASE)
        if not m:
            continue
        tbl = clean_identifier(m.group(1))
        rest = m.group(2).strip()

        # Handle ADD PRIMARY KEY
        mpk = re.search(r"ADD\s+PRIMARY\s+KEY\s*\((.*?)\)", rest, flags=re.IGNORECASE)
        if mpk:
            cols = mpk.group(1).replace("`", "").replace('"', "")
            post_statements.append(f'ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(tbl)} ADD PRIMARY KEY ({cols});')
            continue

        # Handle ADD KEY / ADD INDEX / ADD UNIQUE KEY
        mkey = re.search(r"ADD\s+(UNIQUE\s+KEY|UNIQUE\s+INDEX|KEY|INDEX)\s+`?\"?(\w+)`?\"?\s*\((.*?)\)", rest, flags=re.IGNORECASE)
        if mkey:
            kind = mkey.group(1)
            idx_name = mkey.group(2)
            cols = mkey.group(3).replace("`", "").replace('"', "")
            unique = "UNIQUE " if "UNIQUE" in kind.upper() else ""
            post_statements.append(f'CREATE {unique}INDEX IF NOT EXISTS {quote_ident(idx_name)} ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(tbl)} ({cols});')
            continue

        # Handle ADD CONSTRAINT ... FOREIGN KEY ... REFERENCES ...
        mfk = re.search(r'ADD\s+CONSTRAINT\s+`?\"?(\w+)`?\"?\s+FOREIGN\s+KEY\s*\((.*?)\)\s+REFERENCES\s+`?\"?(\w+)`?\"?\s*\((.*?)\)\s*(ON DELETE\s+\w+)?\s*(ON UPDATE\s+\w+)?', rest, flags=re.IGNORECASE)
        if mfk:
            cname = mfk.group(1)
            cols = mfk.group(2).replace("`", "").replace('"', "")
            ref_table = mfk.group(3)
            ref_cols = mfk.group(4).replace("`", "").replace('"', "")
            ondel = (mfk.group(5) or "").strip()
            onupd = (mfk.group(6) or "").strip()
            
            # Fix known table reference issues
            if ref_table == "registrations":
                ref_table = "users"
            
            post_statements.append(
                f'ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(tbl)} ADD CONSTRAINT {quote_ident(cname)} FOREIGN KEY ({cols}) REFERENCES {quote_ident(TARGET_SCHEMA)}.{quote_ident(ref_table)} ({ref_cols}) {ondel} {onupd};'
            )
            continue

        # Handle MODIFY ... AUTO_INCREMENT (mark column to become serial)
        mmod = re.search(r"MODIFY\s+`?\"?(\w+)`?\"?\s+([^\;]+AUTO_INCREMENT)", rest, flags=re.IGNORECASE)
        if mmod:
            col = mmod.group(1)
            # mark table_autoinc_map so CREATE conversion will create SERIAL instead
            table_autoinc_map.setdefault(tbl, set()).add(col)
            # no immediate statement emitted; conversion will be handled in CREATE
            continue

        # If none matched, comment it out safely
        post_statements.append(f'-- SKIPPED ALTER: {a_clean}')
    return post_statements

def main():
    # read all files and merge into a single string (order matters a bit; sort filenames)
    files = sorted([f for f in os.listdir(INPUT_DIR) if f.endswith(".sql")])
    if not files:
        print(f"[!] No .sql files found in {INPUT_DIR}")
        return

    merged = ""
    for fn in files:
        path = os.path.join(INPUT_DIR, fn)
        with open(path, "r", encoding="utf-8") as fh:
            merged += "\n\n-- file: " + fn + "\n" + fh.read() + "\n\n"

    merged = remove_mysql_noise(merged)

    # find ALTER TABLE statements first to detect MODIFY AUTO_INCREMENT markers
    alters = find_alter_statements(merged)
    table_autoinc_map = {}
    # quick scan to mark auto-increment columns from ALTER MODIFY lines
    for a in alters:
        mmod = re.search(r"MODIFY\s+`?\"?(\w+)`?\"?\s+[^\;]*AUTO_INCREMENT", a, flags=re.IGNORECASE)
        if mmod:
            # find tablename
            mt = re.match(r"ALTER\s+TABLE\s+`?\"?([^`\" ]+)`?\"?", a, flags=re.IGNORECASE)
            if mt:
                tbl = clean_identifier(mt.group(1))
                col = mmod.group(1)
                table_autoinc_map.setdefault(tbl, set()).add(col)

    # extract CREATE TABLE blocks
    create_blocks = find_create_table_blocks(merged)

    created_sql_list = []
    all_index_stmts = []
    all_inline_constraints = []

    for start, end, block_text in create_blocks:
        try:
            created_sql, inline_indexes, inline_constraints = convert_create_block(block_text, table_autoinc_map)
            created_sql_list.append(created_sql)
            all_index_stmts.extend(inline_indexes)
            all_inline_constraints.extend(inline_constraints)
        except Exception as ex:
            created_sql_list.append(f'-- ERROR PARSING CREATE BLOCK: {ex}\n-- original block (truncated):\n' + block_text[:300])
            continue

    # process alters into final ALTER / INDEX / FK statements
    post_alter_stmts = process_alters(alters, table_autoinc_map)

    # Build final output
    out_lines = []
    out_lines.append(f'CREATE SCHEMA IF NOT EXISTS {quote_ident(TARGET_SCHEMA)};')
    out_lines.append(f'SET search_path TO {quote_ident(TARGET_SCHEMA)};')
    out_lines.append("\n-- ===== CREATE TABLES =====\n")
    out_lines.extend(created_sql_list)
    
    # Separate primary key additions from other ALTER statements
    primary_key_stmts = []
    other_alter_stmts = []
    
    for stmt in post_alter_stmts:
        if "ADD PRIMARY KEY" in stmt:
            primary_key_stmts.append(stmt)
        else:
            other_alter_stmts.append(stmt)
    
    out_lines.append("\n-- ===== ADD PRIMARY KEYS =====\n")
    out_lines.extend(primary_key_stmts)
    out_lines.append("\n-- ===== INDEXES from inline definitions =====\n")
    out_lines.extend(all_index_stmts)
    out_lines.append("\n-- ===== INLINE CONSTRAINTS (ENUM CHECKS, etc.) =====\n")
    out_lines.extend(all_inline_constraints)
    out_lines.append("\n-- ===== OTHER ALTER / FK =====\n")
    out_lines.extend(other_alter_stmts)

    # write output
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        out.write("\n\n".join(out_lines))

    print(f"[OK] Written Postgres schema to: {OUTPUT_FILE}")
    print("  - Review enum CHECK constraints and ON UPDATE semantics.")
    print("  - Run with: psql -U <user> -d <db> -f " + OUTPUT_FILE)

if __name__ == "__main__":
    main()
