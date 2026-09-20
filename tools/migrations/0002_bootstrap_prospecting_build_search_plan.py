from django.db import migrations


def bootstrap_tool(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.update_or_create(
        slug="prospecting.build_search_plan",
        defaults={
            "name": "Build Prospecting Search Plan",
            "description": "Deterministic local tool that builds a search-query plan for Prospecting Agent runs.",
            "category": "prospecting",
            "runtime_handler": "prospecting_build_search_plan",
            "is_active": True,
        },
    )


def remove_tool(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.filter(slug="prospecting.build_search_plan").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tools", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(bootstrap_tool, remove_tool),
    ]
