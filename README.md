# mysql2pgsql

A Python utility to **convert MySQL schema dumps into PostgreSQL-compatible schemas**.
It cleans up MySQL-specific syntax, converts types, and generates a `schema_postgres.sql` file that can be loaded directly into PostgreSQL.

---

## ✨ Features

* Cleans MySQL noise:

  * Removes comments, `SET` statements, `ENGINE`, `AUTO_INCREMENT`, `CHARSET`, etc.
* Converts data types:

  * `tinyint(1)` → `BOOLEAN`
  * `int(x)` → `INTEGER`
  * `bigint(x)` → `BIGINT`
  * `decimal(a,b)` → `NUMERIC(a,b)`
  * `double` → `DOUBLE PRECISION`
  * `datetime` → `TIMESTAMP`
  * `enum('a','b')` → `VARCHAR(191)` + `CHECK` constraint
* Converts `AUTO_INCREMENT` columns into `SERIAL`/`BIGSERIAL`.
* Extracts `PRIMARY KEY`, `INDEX`, `UNIQUE`, and `FOREIGN KEY` definitions and rewrites them in PostgreSQL syntax.
* Handles inline constraints and separates them into ALTER statements.
* Generates a single Postgres-ready schema file with:

  1. `CREATE SCHEMA`
  2. `CREATE TABLE`
  3. `PRIMARY KEYS`
  4. `INDEXES`
  5. `CONSTRAINTS`
  6. Other ALTERs

---

## 📂 Project Structure

```
mysql2pg.py         # The converter script
sql/                # Put your MySQL .sql files here
schema_postgres.sql # Generated Postgres schema (output)
```

---

## 🚀 Usage

1. Place all MySQL `.sql` schema files (one per table, or full dumps) inside the `sql/` directory.
2. Run the script:

   ```bash
   python3 mysql2pg.py
   ```
3. The cleaned and converted schema will be saved to:

   ```
   schema_postgres.sql
   ```
4. Load it into PostgreSQL:

   ```bash
   psql -U <username> -d <database> -f schema_postgres.sql
   ```

---

## ⚠️ Notes & Caveats

* Review `enum`-based `CHECK` constraints manually to ensure correctness.
* PostgreSQL does not support `ON UPDATE CURRENT_TIMESTAMP` inline — you may need triggers for equivalent behavior.
* Some MySQL-specific quirks (e.g., unusual collations, special constraints) may require manual adjustment.
* The target schema name defaults to `myapp`. Change it in the script:

  ```python
  TARGET_SCHEMA = "myapp"
  ```

---

## 🛠️ Requirements

* Python 3.6+
* No external dependencies (uses only `os` and `re`).

---

## ✅ Example

If you have a MySQL dump like:

```sql
CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `active` tinyint(1) DEFAULT '1',
  `role` enum('admin','user') NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
```

The script generates:

```sql
CREATE TABLE IF NOT EXISTS "myapp"."users" (
    "id" SERIAL PRIMARY KEY,
    "active" BOOLEAN DEFAULT TRUE,
    "role" VARCHAR(191) NOT NULL
);

ALTER TABLE "myapp"."users" ADD CHECK ("role" IN ('admin','user'));
```
