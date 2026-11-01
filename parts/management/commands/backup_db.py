from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from parts import backup


class Command(BaseCommand):
    help = 'Back up the SQLite database and the media folder (PDF originals) into one verified archive (safe while the app runs).'

    def add_arguments(self, parser):
        parser.add_argument('--label', default='', help='e.g. pre-deploy (lower-case letters, digits, hyphens)')
        parser.add_argument('--prune', action='store_true', help='apply the retention policy afterwards')
        parser.add_argument('--db', default=None)
        parser.add_argument('--media', default=None)
        parser.add_argument('--backup-dir', default=None)

    def handle(self, *args, **opts):
        backup_dir = opts['backup_dir'] or settings.BACKUP_DIR
        try:
            path = backup.create_backup(
                opts['db'] or settings.DATABASES['default']['NAME'], opts['media'] or settings.MEDIA_ROOT,
                backup_dir, label=opts['label'])
            # Prove the archive just written opens and matches its checksums before reporting success.
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                manifest, _ = backup.verify_archive(path, tmp)
            self.stdout.write(f'BACKUP_CREATED {path.name} files={len(manifest["files"])}')
            if opts['prune']:
                removed = backup.prune(backup_dir)
                self.stdout.write(f'PRUNED {len(removed)} old backup(s)')
        except backup.BackupError as e:
            raise CommandError(str(e))
