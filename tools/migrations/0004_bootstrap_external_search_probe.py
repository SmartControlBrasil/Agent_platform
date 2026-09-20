from django.db import migrations


def bootstrap_probe(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.update_or_create(
        slug="prospecting.external_search_probe",
        defaults={
            "name": "External Search Probe",
            "description": "Delegated laboratory tool used to prove external tool execution lifecycle without performing real search.",
            "category": "prospecting",
            "runtime_handler": "",
            "execution_mode": "DELEGATED",
            "is_active": True,
        },
    )
    ToolDefinition.objects.filter(slug="prospecting.build_search_plan").update(execution_mode="LOCAL")


def remove_probe(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.filter(slug="prospecting.external_search_probe").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tools", "0003_tooldefinition_execution_mode_and_more"),
    ]

    operations = [
        migrations.RunPython(bootstrap_probe, remove_probe),
    ]
