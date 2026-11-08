"""Dump all data as JSON for moving it between database engines, without losing precision.

Django's own `dumpdata` cuts every timestamp to milliseconds (its JSON encoder drops the last three digits of the
microseconds). SQLite keeps microseconds, so a plain dumpdata -> loaddata would change every created_at / updated_at.
This command is `dumpdata` with an encoder that keeps all digits. Load the result with the normal `loaddata`.
Content types, permissions and sessions are left out (`migrate` recreates them); foreign keys to them use natural keys.
Used once, for the move to PostgreSQL (ADR 0003); the proof is `data_fingerprint` on both sides.
"""
import datetime

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.serializers import json as django_json


class ExactJSONEncoder(django_json.DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            r = o.isoformat()  # keeps the microseconds
            return r.removesuffix('+00:00') + 'Z' if r.endswith('+00:00') else r
        if isinstance(o, datetime.time) and not isinstance(o, datetime.datetime):
            return o.isoformat() if o.tzinfo is None else super().default(o)
        return super().default(o)


class Command(BaseCommand):
    help = 'dumpdata as JSON with full timestamp precision (for the SQLite -> PostgreSQL move).'

    def handle(self, *args, **options):
        original = django_json.DjangoJSONEncoder
        django_json.DjangoJSONEncoder = ExactJSONEncoder  # the serializer looks the name up when it starts
        try:
            call_command(
                'dumpdata', use_natural_foreign_keys=True, use_natural_primary_keys=True,
                exclude=['contenttypes', 'auth.permission', 'sessions'], format='json', stdout=self.stdout,
            )
        finally:
            django_json.DjangoJSONEncoder = original
