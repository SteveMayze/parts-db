import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from parts import backup


class Command(BaseCommand):
    help = ('Verify a backup archive, and optionally restore it. Without --target-db it only verifies '
            '(checksums, database integrity, row counts) and changes nothing.')

    def add_arguments(self, parser):
        parser.add_argument('archive', help='backup file name (looked up in the backup directory) or a full path')
        parser.add_argument('--target-db', default=None)
        parser.add_argument('--target-media', default=None)
        parser.add_argument('--force', action='store_true', help='move existing targets aside instead of refusing')
        parser.add_argument('--backup-dir', default=None)

    def handle(self, *args, **opts):
        archive = Path(opts['archive'])
        if not archive.is_absolute():
            archive = Path(opts['backup_dir'] or settings.BACKUP_DIR) / archive.name
        try:
            if not opts['target_db']:
                with tempfile.TemporaryDirectory() as tmp:
                    manifest, _ = backup.verify_archive(archive, tmp)
                counts = manifest['row_counts']
                media = [f for f in manifest['files'] if f.startswith(backup.MEDIA + '/')]
                self.stdout.write('VERIFIED integrity=ok checksums=ok files=%d media_files=%d tables=%d created=%s' % (
                    len(manifest['files']), len(media), len(counts), manifest.get('created_utc')))
                self.stdout.write('ROWS ' + ' '.join(f'{t}={n}' for t, n in sorted(counts.items()) if n))
                return
            if not opts['target_media']:
                raise CommandError('--target-media is required together with --target-db.')
            result = backup.restore_archive(archive, opts['target_db'], opts['target_media'], force=opts['force'])
            self.stdout.write(f'RESTORED created={result["manifest"].get("created_utc")}')
            for p in result['moved_aside']:
                self.stdout.write(f'MOVED_ASIDE {p}')
        except backup.BackupError as e:
            raise CommandError(str(e))
