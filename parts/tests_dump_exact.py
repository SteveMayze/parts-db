import datetime
import io
import json

from django.core import serializers
from django.core.management import call_command
from django.test import TestCase

from parts.models import Supplier


class DumpExactTests(TestCase):
    def test_microseconds_survive_dump_and_load(self):
        when = datetime.datetime(2026, 11, 1, 9, 40, 52, 123456, tzinfo=datetime.timezone.utc)
        supplier = Supplier.objects.create(name='Conrad')
        Supplier.objects.filter(pk=supplier.pk).update(created_at=when, updated_at=when)
        out = io.StringIO()
        call_command('dump_exact', stdout=out)
        self.assertIn('2026-11-01T09:40:52.123456Z', out.getvalue())
        json.loads(out.getvalue())  # valid JSON

        Supplier.objects.all().delete()
        for obj in serializers.deserialize('json', out.getvalue()):
            obj.save()
        self.assertEqual(Supplier.objects.get(name='Conrad').created_at, when)

    def test_stock_dumpdata_would_lose_them(self):
        """Documents why the command exists: plain dumpdata cuts to milliseconds."""
        when = datetime.datetime(2026, 11, 1, 9, 40, 52, 123456, tzinfo=datetime.timezone.utc)
        supplier = Supplier.objects.create(name='Conrad')
        Supplier.objects.filter(pk=supplier.pk).update(created_at=when)
        out = io.StringIO()
        call_command('dumpdata', 'parts.supplier', stdout=out)
        self.assertNotIn('123456', out.getvalue())
