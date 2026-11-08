"""Engine-neutral fingerprint of the data: row count and a content hash per model, computed through the ORM.

Rows are read in primary-key order and every value is written in one normalised text form, so the same data gives
the same hash in SQLite and in PostgreSQL. Only counts and hashes are printed, never a value. Content types and
permissions are recreated by `migrate` with different ids on each database, so they are left out, and a reference to
one of them is compared by its natural key (app.model, app.model.codename) instead of its id. Sessions are left out
too (short-lived by nature). Compare two runs with:  diff <(manage.py data_fingerprint) <(manage.py data_fingerprint)
"""
import datetime
import decimal
import hashlib

from django.apps import apps
from django.core.management.base import BaseCommand

SKIP = {'contenttypes.contenttype', 'auth.permission', 'sessions.session'}


def _natural_resolvers():
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType
    cts = {c.pk: f'{c.app_label}.{c.model}' for c in ContentType.objects.all()}
    perms = {p.pk: f'{cts.get(p.content_type_id, "?")}.{p.codename}' for p in Permission.objects.all()}
    return {ContentType: cts, Permission: perms}


def _normal(value):
    if value is None:
        return 'N'
    if isinstance(value, bool):
        return f'b{int(value)}'
    if isinstance(value, decimal.Decimal):
        return 'd' + format(value.normalize(), 'f')
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc)
        return 't' + value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return 't' + value.isoformat()
    if isinstance(value, float):
        return 'f' + repr(value)
    if isinstance(value, (bytes, memoryview)):
        return 'x' + bytes(value).hex()
    return 's' + str(value)


def fingerprint(include_all=False):
    """[(model label, row count, sha256)] and the overall sha256."""
    resolvers = _natural_resolvers()
    rows_out = []
    for model in sorted(apps.get_models(include_auto_created=True), key=lambda m: m._meta.label_lower):
        label = model._meta.label_lower
        if label in SKIP and not include_all:
            continue
        fields = [f for f in model._meta.concrete_fields]
        lookups = {f.attname: resolvers.get(f.related_model) for f in fields if f.is_relation}
        digest = hashlib.sha256()
        count = 0
        for row in model._base_manager.order_by('pk').values_list(*[f.attname for f in fields]).iterator(chunk_size=500):
            count += 1
            cells = []
            for f, value in zip(fields, row):
                mapping = lookups.get(f.attname)
                if mapping is not None and value is not None:
                    value = mapping.get(value, value)
                cells.append(f'{f.attname}={_normal(value)}')
            digest.update('\x1f'.join(cells).encode('utf-8') + b'\x1e')
        rows_out.append((label, count, digest.hexdigest()))
    total = hashlib.sha256()
    for label, count, h in rows_out:
        total.update(f'{label}:{count}:{h}\n'.encode())
    return rows_out, total.hexdigest()


class Command(BaseCommand):
    help = 'Print row count and content hash per model (no values), the same on SQLite and PostgreSQL.'

    def add_arguments(self, parser):
        parser.add_argument('--include-all', action='store_true', help='also content types, permissions and sessions')

    def handle(self, *args, **opts):
        rows, total = fingerprint(opts['include_all'])
        for label, count, h in rows:
            self.stdout.write(f'{label} rows={count} sha256={h}')
        self.stdout.write(f'TOTAL rows={sum(c for _, c, _ in rows)} sha256={total}')
