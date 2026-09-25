import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("prospecting", "0003_searchrunexecutionattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="prospect",
            name="priority",
            field=models.CharField(
                choices=[("UNSET", "—"), ("LOW", "Baixa"), ("MEDIUM", "Média"), ("HIGH", "Alta")],
                default="UNSET",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="prospect",
            name="qualification_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="prospect",
            name="qualification_status",
            field=models.CharField(
                choices=[
                    ("UNQUALIFIED", "Não qualificado"),
                    ("QUALIFIED", "Qualificado"),
                    ("NOT_A_FIT", "Fora do perfil"),
                    ("ON_HOLD", "Em espera"),
                ],
                default="UNQUALIFIED",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="prospect",
            name="qualified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="prospect",
            name="qualified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="qualified_prospects",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="prospect",
            index=models.Index(fields=["tenant", "qualification_status"], name="prospecting_tenant__8a0f1d_idx"),
        ),
        migrations.AddIndex(
            model_name="prospect",
            index=models.Index(fields=["tenant", "priority"], name="prospecting_tenant__c4e2a9_idx"),
        ),
    ]
