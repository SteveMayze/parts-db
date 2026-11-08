import tempfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.core.management.base import BaseCommand, CommandError

from parts import backup


class Command(BaseCommand):
    help = ('Verify a backup archive, and optionally restore it. Without a target it only verifies '
            '(checksums, database integrity, row counts) and changes nothing. SQLite: --target-db FILE --target-media DIR. '
            'PostgreSQL: --new-database NAME --target-media DIR restores into a new, empty database; adding --swap '
            '--confirm-swap SWAP also renames the live database aside and puts the restored one in its place.')

    def add_arguments(self, parser):
        parser.add_argument('archive', help='backup file name (looked up in the backup directory) or a full path')
        parser.add_argument('--target-db', default=None, help='SQLite only')
        parser.add_argument('--new-database', default=None, help='PostgreSQL only: name of the new, empty database ("auto": <live>_restore_<time>)')
        parser.add_argument('--drill', action='store_true', help='PostgreSQL only: restore into a scratch database, compare the counts, drop the scratch database; live data untouched')
        parser.add_argument('--swap', action='store_true', help='PostgreSQL only: replace the live database (renamed aside, never dropped)')
        parser.add_argument('--confirm-swap', default='', help='must be SWAP together with --swap')
        parser.add_argument('--target-media', default=None)
        parser.add_argument('--force', action='store_true', help='move existing targets aside instead of refusing')
        parser.add_argument('--backup-dir', default=None)

    def handle(self, *args, **opts):
        archive = Path(opts['archive'])
        if not archive.is_absolute():
            archive = Path(opts['backup_dir'] or settings.BACKUP_DIR) / archive.name
        try:
            if opts['drill']:
                if not settings.USING_POSTGRES:
                    raise CommandError('--drill needs the PostgreSQL settings (DJANGO_DB_HOST).')
                result = backup.drill_pg_archive(archive, connection.settings_dict)
                self.stdout.write(f'DRILL_RESTORED created={result["manifest"].get("created_utc")} tables={len(result["manifest"]["row_counts"])} scratch_database_dropped=yes')
                return
            if not opts['target_db'] and not opts['new_database']:
                with tempfile.TemporaryDirectory() as tmp:
                    manifest, _ = backup.verify_archive(archive, tmp)
                counts = manifest['row_counts']
                media = [f for f in manifest['files'] if f.startswith(backup.MEDIA + '/')]
                self.stdout.write('VERIFIED integrity=ok checksums=ok files=%d media_files=%d tables=%d engine=%s created=%s' % (
                    len(manifest['files']), len(media), len(counts), manifest.get('engine', 'sqlite'), manifest.get('created_utc')))
                self.stdout.write('ROWS ' + ' '.join(f'{t}={n}' for t, n in sorted(counts.items()) if n))
                return
            if opts['target_db'] and opts['new_database']:
                raise CommandError('Use --target-db (SQLite) or --new-database (PostgreSQL), not both.')
            if not opts['target_media']:
                raise CommandError('--target-media is required together with a restore target.')
            if opts['new_database']:
                if not settings.USING_POSTGRES:
                    raise CommandError('--new-database needs the PostgreSQL settings (DJANGO_DB_HOST).')
                if opts['swap'] and opts['confirm_swap'] != 'SWAP':
                    raise CommandError('--swap replaces the live database: add --confirm-swap SWAP to confirm.')
                db = connection.settings_dict
                new_db = opts['new_database']
                if new_db == 'auto':
                    import datetime
                    new_db = f"{db['NAME']}_restore_{datetime.datetime.now(datetime.timezone.utc):%Y%m%d_%H%M%S}"
                result = backup.restore_pg_archive(
                    archive, db, new_db, opts['target_media'],
                    force=opts['force'], swap=opts['swap'])
                self.stdout.write(f'RESTORED created={result["manifest"].get("created_utc")} database={result["new_db"]}')
                if result['swapped_aside_db']:
                    self.stdout.write(f'SWAPPED live database kept as {result["swapped_aside_db"]}')
            else:
                if opts['swap']:
                    raise CommandError('--swap is only for PostgreSQL.')
                result = backup.restore_archive(archive, opts['target_db'], opts['target_media'], force=opts['force'])
                self.stdout.write(f'RESTORED created={result["manifest"].get("created_utc")}')
            for p in result['moved_aside']:
                self.stdout.write(f'MOVED_ASIDE {p}')
        except backup.BackupError as e:
            raise CommandError(str(e))
