# Generated by Django 2.2 on 2019-05-07 18:44

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('app_tpnm', '0008_auto_20190507_1810'),
    ]

    operations = [
        migrations.AddField(
            model_name='comment',
            name='article',
            field=models.ForeignKey(default=0, on_delete=django.db.models.deletion.CASCADE, to='app_tpnm.Article'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='comment',
            name='edited',
            field=models.DateTimeField(auto_now=True),
        ),
    ]