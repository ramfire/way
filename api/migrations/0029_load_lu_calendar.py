from django.db import migrations
from datetime import date, timedelta


def _easter(year):
    # Computus grégorien (Meeus / Jones / Butcher)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


FIXED = [
    (1, 1, "Nouvel An", False),
    (5, 1, "Fête du Travail", False),
    (5, 9, "Journée de l'Europe", False),
    (6, 23, "Fête Nationale", False),
    (8, 15, "Assomption", False),
    (11, 1, "Toussaint", False),
    (12, 24, "Réveillon de Noël", True),   # B — après-midi → jour fermé
    (12, 25, "Noël", False),
    (12, 26, "Saint-Étienne", False),
]

YEARS = range(2026, 2031)   # 2026 → 2030 inclus


def load_lu(apps, schema_editor):
    BusinessCalendar = apps.get_model("api", "BusinessCalendar")
    CalendarHoliday = apps.get_model("api", "CalendarHoliday")
    SubTenant = apps.get_model("api", "SubTenant")

    st = SubTenant.objects.order_by("pk").first()
    if st is None:
        print("WARNING: no SubTenant found — LU calendar NOT loaded.")
        return

    cal, _ = BusinessCalendar.objects.get_or_create(
        code="LU",
        defaults={"label": "Luxembourg (ABBL)", "sub_tenant": st},
    )

    rows = []
    for y in YEARS:
        easter = _easter(y)
        mobile = [
            (easter - timedelta(days=2), "Vendredi Saint", True),   # B
            (easter + timedelta(days=1), "Lundi de Pâques", False),
            (easter + timedelta(days=39), "Ascension", False),
            (easter + timedelta(days=50), "Lundi de Pentecôte", False),
        ]
        for mo, da, label, is_b in FIXED:
            rows.append((date(y, mo, da), label, is_b))
        for dt, label, is_b in mobile:
            rows.append((dt, label, is_b))

    for dt, label, is_b in rows:
        CalendarHoliday.objects.get_or_create(
            business_calendar=cal,
            date=dt,
            defaults={"label": label, "is_bank_holiday": is_b, "sub_tenant": st},
        )


def unload_lu(apps, schema_editor):
    BusinessCalendar = apps.get_model("api", "BusinessCalendar")
    BusinessCalendar.objects.filter(code="LU").delete()   # CASCADE → holidays


class Migration(migrations.Migration):
    dependencies = [("api", "0028_business_calendar")]
    operations = [migrations.RunPython(load_lu, unload_lu)]
