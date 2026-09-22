from django.db import migrations


def bootstrap_google_maps_search(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.update_or_create(
        slug="prospecting.search_google_maps",
        defaults={
            "name": "Search Google Maps",
            "description": "Delegated browser-executor contract for Google Maps prospecting search. This tool defines input/output validation only and does not implement scraping.",
            "category": "prospecting",
            "runtime_handler": "",
            "execution_mode": "DELEGATED",
            "is_active": True,
        },
    )


def remove_google_maps_search(apps, schema_editor):
    ToolDefinition = apps.get_model("tools", "ToolDefinition")
    ToolDefinition.objects.filter(slug="prospecting.search_google_maps").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tools", "0005_toolexecutorcredential_toolexecutorpairingrequest"),
    ]

    operations = [
        migrations.RunPython(bootstrap_google_maps_search, remove_google_maps_search),
    ]
