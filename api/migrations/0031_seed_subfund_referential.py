from django.db import migrations


def seed(apps, schema_editor):
    Referential = apps.get_model("api", "Referential")
    SubTenant = apps.get_model("api", "SubTenant")
    st = SubTenant.objects.order_by("pk").first()
    if st is None:
        print("WARNING: no SubTenant found — 'subfund' referential NOT seeded.")
        return
    Referential.objects.get_or_create(
        code="subfund",
        defaults={"label": "Sous-fonds / compartiment",
                  "absence_policy": "candidate",
                  "sub_tenant": st},
    )


def unseed(apps, schema_editor):
    Referential = apps.get_model("api", "Referential")
    Referential.objects.filter(code="subfund").delete()


class Migration(migrations.Migration):
    dependencies = [("api", "0030_subfund_referential")]
    operations = [migrations.RunPython(seed, unseed)]
