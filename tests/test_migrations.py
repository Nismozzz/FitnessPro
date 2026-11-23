"""Exercise real CLI migrations in isolated databases, never the developer DB."""
import os
import subprocess
import sys


def cli(url, *args):
    return subprocess.run([sys.executable, '-m', 'alembic', *args],
                          env={**os.environ, 'DATABASE_URL': url},
                          capture_output=True, text=True, check=True)


def test_upgrade_downgrade_and_schema_parity(tmp_path):
    import sqlite3

    path = tmp_path / 'migration.db'
    url = f'sqlite+aiosqlite:///{path}'
    cli(url, 'upgrade', '0001')
    with sqlite3.connect(path) as db:
        assert db.execute('select version_num from alembic_version').fetchone() == ('0001',)
        db.execute("insert into users values ('test', 'tester', 'hash', '2026-11-18')")
        db.execute("insert into user_profiles (user_id, goal, updated_at) values ('test', 'fat_loss', '2026-11-18')")
    cli(url, 'upgrade', 'head')
    cli(url, 'upgrade', 'head')
    cli(url, 'check')
    with sqlite3.connect(path) as db:
        assert db.execute('select version_num from alembic_version').fetchone() == ('0002',)
        assert db.execute('select username from users').fetchone() == ('tester',)
        assert db.execute(
            'select goal, dietary_preferences, dietary_habits, dietary_restrictions from user_profiles'
        ).fetchone() == ('fat_loss', None, None, None)
    cli(url, 'downgrade', '0001')
    with sqlite3.connect(path) as db:
        assert db.execute('select goal from user_profiles').fetchone() == ('fat_loss',)
        assert all(not row[1].startswith('dietary_') for row in db.execute('pragma table_info(user_profiles)'))
    cli(url, 'downgrade', 'base')
    with sqlite3.connect(path) as db:
        assert db.execute("select name from sqlite_master where type='table'").fetchall() == [('alembic_version',)]
    cli(url, 'upgrade', 'head')


def test_adopt_legacy_preserves_data_and_adds_profile_fields(tmp_path):
    import sqlite3

    path = tmp_path / 'legacy.db'
    url = f'sqlite+aiosqlite:///{path}'
    cli(url, 'upgrade', '0001')
    with sqlite3.connect(path) as db:
        db.execute('drop table alembic_version')
        db.execute('alter table user_profiles drop column training_location')
        db.execute('alter table user_profiles drop column excluded_equipment')
        db.execute("insert into users values ('test', 'tester', 'hash', '2026-11-18')")
    subprocess.run([sys.executable, '-m', 'scripts.adopt_legacy_db'],
                   env={**os.environ, 'DATABASE_URL': url}, capture_output=True, text=True, check=True)
    cli(url, 'upgrade', 'head')
    cli(url, 'check')
    with sqlite3.connect(path) as db:
        assert db.execute('select username from users').fetchone() == ('tester',)
