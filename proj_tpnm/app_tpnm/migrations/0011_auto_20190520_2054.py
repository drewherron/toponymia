# Generated by Django 2.2.1 on 2019-05-20 20:54

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('app_tpnm', '0010_auto_20190519_0026'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='edit',
            name='article',
        ),
        migrations.RemoveField(
            model_name='edit',
            name='name',
        ),
        migrations.CreateModel(
            name='ArticleName',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tpnm_id', models.CharField(max_length=500)),
                ('name', models.CharField(max_length=200)),
                ('article', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='article_names', to='app_tpnm.Article')),
            ],
        ),
        migrations.AddField(
            model_name='edit',
            name='article_name',
            field=models.ForeignKey(default=0, on_delete=django.db.models.deletion.CASCADE, related_name='edits', to='app_tpnm.ArticleName'),
            preserve_default=False,
        ),
    ]
