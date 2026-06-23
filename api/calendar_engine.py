"""Comptage de jours ouvrés sur un ``BusinessCalendar`` (§3.8).

Utilitaire **pur** : ne lit que le référentiel calendrier (week-ends déduits,
``CalendarHoliday``, ``CalendarException``) et ne calcule QUE des jours ouvrés.
Précédence (documentée sur ``BusinessCalendar``) : exception calendrier > férié de
place > week-end. La portée *sous-fonds* des exceptions est différée (pas encore
modélisée). Aucune écriture, aucune dépendance au moteur d'identification.
"""

from datetime import timedelta


class BusinessDays:
    """Pré-charge fériés/exceptions d'UN calendrier puis répond **en mémoire** (pas de
    N+1 quand on expanse sur une fenêtre). Réutiliser une instance par calendrier."""

    def __init__(self, calendar):
        self.calendar = calendar
        self.holidays = set(calendar.holidays.values_list('date', flat=True))
        # date → is_open : override bidirectionnel, prioritaire sur férié/week-end.
        self.exceptions = dict(calendar.exceptions.values_list('date', 'is_open'))

    def is_business_day(self, d):
        if d in self.exceptions:
            return self.exceptions[d]      # exception ouvre OU ferme explicitement
        if d in self.holidays:
            return False                   # férié de place
        return d.weekday() < 5             # week-end déduit (sam=5, dim=6)

    def add(self, d, n):
        """``d`` décalé de ``n`` jours OUVRÉS (signe = sens). ``n == 0`` → ``d``
        inchangé. Le point de départ n'est jamais compté ; on atterrit toujours sur
        un jour ouvré."""
        if n == 0:
            return d
        step = 1 if n > 0 else -1
        remaining = abs(n)
        cur = d
        while remaining:
            cur += timedelta(days=step)
            if self.is_business_day(cur):
                remaining -= 1
        return cur

    def subtract(self, d, n):
        """``d`` − ``n`` jours ouvrés (``n >= 0``) — recul (ex. delivery → valo)."""
        return self.add(d, -n)


def is_business_day(calendar, d):
    """Helper module (construit un calculateur à la volée) — pratique en test /
    appel unitaire. Pour une expansion, instancier ``BusinessDays`` et le réutiliser."""
    return BusinessDays(calendar).is_business_day(d)


def add_business_days(calendar, d, n):
    """``calendar``-aware : ``d`` + ``n`` jours ouvrés (cf. ``BusinessDays.add``)."""
    return BusinessDays(calendar).add(d, n)
