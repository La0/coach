# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0011_athlete_display_help'),
    ]

    operations = [
        migrations.AddField(
            model_name='athlete',
            name='paymill_id',
            field=models.CharField(max_length=50, null=True, blank=True),
        ),
    ]
