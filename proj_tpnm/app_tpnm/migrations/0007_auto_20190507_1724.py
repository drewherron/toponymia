# Generated by Django 2.2 on 2019-05-07 17:24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_tpnm', '0006_auto_20190506_2314'),
    ]

    operations = [
        migrations.AlterField(
            model_name='language',
            name='family_id',
            field=models.PositiveSmallIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='language',
            name='father_id',
            field=models.PositiveSmallIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='language',
            name='status',
            field=models.CharField(max_length=200, null=True),
        ),
    ]
