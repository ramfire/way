"""Loader **APINAV** : charge un fichier LU_PO_NAV dans Acta, uniquement par l'API.

Acta fait foi sur le référentiel ; il refuse une VNI tant que le compartiment, la
classe, leurs codes externes ``SOURCE_CODE`` (source ``ACTA_SOURCE``) et le lien
``SHARE_CLASS_OF`` n'existent pas à ``nav_date``. D'où trois temps :

  1. **plan** : lecture du fichier (colonnes nommées par ``feed.layout.header``),
     comparaison aux codes déjà connus d'Acta → entités/liens à créer + VNI ;
  2. **entités** (CRUD, une par une — l'ingest SDF ne crée pas d'entités) :
     compartiment ``SUB_FUND`` (sans umbrella), classe ``SHARE_CLASS``, code externe
     de chacun, lien ``SHARE_CLASS_OF``. Un code externe refusé → l'entité tout
     juste créée est clôturée (``DELETE`` = ``end_validity``) pour ne pas laisser
     d'orphelin qu'un rejeu dupliquerait ;
  3. **VNI** : ``POST /api/v1/ingest/`` (``SHARE_CLASS_NAV``, lots ≤ 1000), rejet
     unitaire et idempotence côté Acta.
     Le lot porte ``file_ref`` (``alfaway:<id>:<path>``) et chaque VNI sa ligne
     d'origine (``source_line_no``) ; Acta les trace dans son journal
     (``/api/v1/nav-imports/``), **relu** après l'ingest : imports créés, NAV
     reçues / stockées / inchangées / refusées, lignes refusées et motif ;
  4. **compte** (dernier contrôle, ``acta_count``) : chaque VNI envoyée doit revenir
     dans la réponse de l'API (``api_count_mismatch`` sinon) et, si le journal a été
     relu, y être journalisée (``acta_journal_mismatch``) → ``warning_action``.

Taux de change : le fichier donne « devise de la part pour 1 unité de devise du
fonds » ; Acta attend la **cotation de marché** (devise de base = plus haute
``quote_priority``) et choisit × ou ÷ lui-même. Classe base → Acta multiplie →
taux = 1 / taux fichier ; sinon Acta divise → taux = taux fichier.

``dry_run`` n'écrit rien (ni Acta ni Event). AlfaWay ne fait **aucun contrôle
métier** (arithmétique VNI/TNA, cohérence de taux…) : c'est le rôle de Trail ; le
plan ne vérifie que la forme (champs présents, nombres et dates lisibles, devises
connues d'Acta).
"""
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .acta import ActaClient, ActaError, SOURCE_CODE
from .models import (
    Event, ReceivedFile, current_control_rollup, refresh_control_class, run_scope,
)

logger = logging.getLogger(__name__)

STAGE = 'load'
CTRL_ENTITIES = 'acta_entities'
CTRL_NAV = 'acta_nav'
CTRL_PLAN = 'acta_plan'
CTRL_COUNT = 'acta_count'

NAV_TYPES = {'O': 'OFFICIAL', 'E': 'ESTIMATED', 'T': 'TECHNICAL'}
INGEST_BATCH = 1000


def _dec(value):
    v = (value or '').strip()
    if v.startswith('.'):
        v = '0' + v
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _iso(yyyymmdd):
    s = (yyyymmdd or '').strip()
    return f'{s[0:4]}-{s[4:6]}-{s[6:8]}' if len(s) == 8 and s.isdigit() else None


def _plain(d):
    """Décimal → chaîne SDF ``^\\d+(\\.\\d+)?$`` (pas d'exposant, pas de signe)."""
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


@dataclass
class Plan:
    funds: dict = field(default_factory=OrderedDict)     # code → attrs
    classes: dict = field(default_factory=OrderedDict)   # share_code → attrs
    navs: list = field(default_factory=list)              # records SDF
    errors: list = field(default_factory=list)            # (ligne, motif) bloquants


def _rows(rf, feed):
    from .parsing import _fetch_bytes
    layout = feed.layout or {}
    names = (layout.get('header') or {}).get('columns') or []
    text = _fetch_bytes(rf).decode(layout.get('encoding') or 'utf-8')
    delim = layout.get('delimiter') or ','
    for i, line in enumerate(l for l in text.splitlines() if l.strip()):
        yield i + 1, dict(zip(names, line.split(delim)))


def build_plan(rf, feed, client, known_codes=None, currencies=None):
    """Lit le fichier et calcule ce qu'il faut créer / envoyer (aucune écriture)."""
    known = known_codes if known_codes is not None else client.source_codes(
        settings.ACTA_SOURCE)
    ccys = currencies if currencies is not None else client.currencies()
    plan = Plan()
    for n, r in _rows(rf, feed):
        fund, share = r.get('Fund_code', '').strip(), r.get('Share_code', '').strip()
        nav_date = _iso(r.get('NAV_date'))
        fund_ccy, class_ccy = r.get('Fund_currency', '').strip(), r.get('Share_currency', '').strip()
        nav_type = NAV_TYPES.get(r.get('NAV_Type', '').strip())
        units, nav = _dec(r.get('Nb_of_outstanding_shares')), _dec(r.get('Price_share_CCY'))
        amt_class = _dec(r.get('TNA_of_the_share_in_share_CCY'))
        amt_fund = _dec(r.get('TNA_of_the_share_in_fund_CCY'))
        fx_file = _dec(r.get('FX_Rate'))
        missing = [k for k, v in (('Fund_code', fund), ('Share_code', share),
                                  ('NAV_date', nav_date), ('NAV_Type', nav_type),
                                  ('units', units), ('nav', nav), ('tna_class', amt_class),
                                  ('tna_fund', amt_fund), ('fx', fx_file)) if not v]
        if missing:
            plan.errors.append((n, f'champs invalides/absents : {missing}'))
            continue
        if fund_ccy not in ccys or class_ccy not in ccys:
            plan.errors.append((n, f'devise inconnue d’Acta : {fund_ccy}/{class_ccy}'))
            continue

        # Taux au sens Acta (cf. docstring).
        if fund_ccy == class_ccy:
            fx = Decimal(1)
        elif ccys[class_ccy]['quote_priority'] > ccys[fund_ccy]['quote_priority']:
            fx = (Decimal(1) / fx_file).quantize(Decimal('0.000001'))
        else:
            fx = fx_file

        f = plan.funds.setdefault(fund, {
            'code': fund, 'name': r.get('Fund_name', '').strip(), 'currency': fund_ccy,
            'uid': known.get(fund), 'valid_from': nav_date})
        f['valid_from'] = min(f['valid_from'], nav_date)
        c = plan.classes.setdefault(share, {
            'code': share, 'fund': fund, 'currency': class_ccy,
            'isin': r.get('ISIN_code', '').strip() or None,
            'name': f"{f['name']} — {r.get('Share_type', '').strip() or share}",
            'uid': known.get(share), 'valid_from': nav_date})
        c['valid_from'] = min(c['valid_from'], nav_date)
        f['valid_from'] = min(f['valid_from'], c['valid_from'])

        plan.navs.append({
            'record_type': 'SHARE_CLASS_NAV',
            'source_record_id': f'{share}-{nav_date}-{nav_type}',
            'fund': {'code': fund}, 'share_class': {'code': share},
            'nav_date': nav_date, 'nav_type': nav_type,
            'fund_currency': fund_ccy, 'class_currency': class_ccy,
            'units_outstanding': _plain(units), 'nav_per_unit': _plain(nav),
            'total_nav_class_ccy': _plain(amt_class),
            'total_nav_fund_ccy': _plain(amt_fund), 'fx_rate': _plain(fx),
            'source_line_no': n,
        })
    return plan


# --- écriture ---------------------------------------------------------------
def _create_entity(client, payload, code, created, failures):
    """Entité + son code externe ; rollback (clôture) si le code est refusé."""
    status, body = client.send_json('POST', '/api/v1/entities/', payload)
    if status != 201:
        failures.append({'code': code, 'step': 'entity', 'status': status, 'detail': body})
        return None
    uid = body['uid']
    status, ext = client.send_json('POST', '/api/v1/entity-external-codes/', {
        'entity': uid, 'source': settings.ACTA_SOURCE, 'code_type': SOURCE_CODE,
        'code': code, 'valid_from': payload['valid_from']})
    if status != 201:
        client.send_json('DELETE', f'/api/v1/entities/{uid}/', None)
        failures.append({'code': code, 'step': 'external_code', 'status': status,
                         'detail': ext, 'rolled_back': uid})
        return None
    created.append({'code': code, 'uid': uid, 'type': payload['entity_type']})
    return uid


def file_ref_of(rf):
    """Référence du fichier chez AlfaWay, tracée par Acta (``NavImport.file_ref``)."""
    return f'alfaway:{rf.pk}:{rf.path}'[:512]


def _last_import_id(client, file_ref):
    """Plus grand id d'import déjà journalisé pour ce fichier (0 si aucun)."""
    return max((i['id'] for i in client.nav_imports(file_ref)), default=0)


def read_journal(client, file_ref, after_id, sent):
    """Relit dans Acta le journal des imports créés par ce chargement (id > after_id).

    Rapprochement **technique** (transport), pas métier : Acta a-t-il journalisé
    autant de NAV qu'envoyées, et lesquelles a-t-il refusées (ligne d'origine, motif).
    ``mismatch`` si le nombre de NAV journalisées diffère du nombre envoyé."""
    imports = sorted((i for i in client.nav_imports(file_ref) if i['id'] > after_id),
                     key=lambda i: i['id'])
    totals = {k: sum(i[k] for i in imports) for k in ('rows', 'stored', 'unchanged', 'rejected')}
    rejected_lines = []
    for i in imports:
        if i['rejected']:
            rejected_lines += [
                {'import': i['id'], 'line_no': l['line_no'],
                 'source_line_no': l.get('source_line_no'),
                 'source_record_id': l.get('source_record_id'), 'reason': l.get('reason')}
                for l in client.nav_import_lines(i['id'], 'REJECTED')]
    return {'imports': [i['id'] for i in imports], **totals,
            'rejected_lines': rejected_lines, 'mismatch': totals['rows'] != sent}


def apply_plan(client, plan, file_ref=None):
    created, links, failures = [], [], []
    for f in plan.funds.values():
        if f['uid'] is None:
            f['uid'] = _create_entity(client, {
                'name': f['name'] or f['code'], 'entity_type': 'SUB_FUND',
                'currency': f['currency'], 'valid_from': f'{f["valid_from"]}T00:00:00Z',
            }, f['code'], created, failures)
    for c in plan.classes.values():
        fund = plan.funds[c['fund']]
        if fund['uid'] is None:
            continue
        new_class = c['uid'] is None
        if new_class:
            payload = {'name': c['name'], 'entity_type': 'SHARE_CLASS',
                       'currency': c['currency'],
                       'valid_from': f'{fund["valid_from"]}T00:00:00Z'}
            if c['isin']:
                payload['isin'] = c['isin']
            c['uid'] = _create_entity(client, payload, c['code'], created, failures)
            if c['uid'] is None:
                continue
        if new_class or c['uid'] not in client.share_classes_of(fund['uid']):
            status, body = client.send_json('POST', '/api/v1/entity-relationships/', {
                'parent': fund['uid'], 'child': c['uid'],
                'relationship_type': 'SHARE_CLASS_OF',
                'valid_from': f'{fund["valid_from"]}T00:00:00Z'})
            if status == 201:
                links.append({'fund': fund['code'], 'class': c['code']})
            else:
                failures.append({'code': c['code'], 'step': 'link', 'status': status,
                                 'detail': body})

    counts, rejected = {}, []
    after_id = None
    if file_ref and plan.navs:
        try:
            after_id = _last_import_id(client, file_ref)
        except ActaError as e:
            logger.warning('Journal Acta illisible avant ingest (%s) : %s', file_ref, e)
    envelope = {'schema_version': '1', 'source': settings.ACTA_SOURCE}
    if file_ref:
        envelope['file_ref'] = file_ref
    returned, missing = 0, []
    for i in range(0, len(plan.navs), INGEST_BATCH):
        records = plan.navs[i:i + INGEST_BATCH]
        status, body = client.send_json('POST', '/api/v1/ingest/', {
            **envelope, 'records': records})
        if status != 200:
            raise ActaError(f'ingest refusé (HTTP {status}) : {str(body)[:500]}')
        for k, v in (body.get('counts') or {}).items():
            counts[k] = counts.get(k, 0) + v
        results = body.get('results') or []
        rejected += [r for r in results if r.get('status') == 'rejected']
        # Compte au retour de l'API : un résultat par enregistrement envoyé.
        returned += len(results)
        echoed = {r.get('source_record_id') for r in results}
        missing += [r['source_record_id'] for r in records if r['source_record_id'] not in echoed]
    # Relecture du journal Acta : jamais bloquante (Acta antérieur à 0020, réseau…).
    journal = None
    if after_id is not None:
        try:
            journal = read_journal(client, file_ref, after_id, len(plan.navs))
        except ActaError as e:
            journal = {'unavailable': str(e)[:300]}
    return {'created': created, 'links': links, 'failures': failures,
            'nav_counts': counts, 'nav_rejected': rejected, 'journal': journal,
            'sent': len(plan.navs), 'returned': returned, 'missing': missing}


def count_check(res):
    """Dernier contrôle du chargement : **compte des enregistrements au retour de
    l'API**. Chaque VNI envoyée doit revenir avec un résultat (``results`` de
    l'ingest, même ``source_record_id``) ; et, si le journal Acta a pu être relu, il
    doit avoir journalisé autant de VNI qu'envoyées. → ``(ok, detail)``."""
    journal = res['journal'] or {}
    detail = {'sent': res['sent'], 'returned': res['returned'],
              'missing': res['missing'][:50], 'journal_rows': journal.get('rows')}
    if res['returned'] != res['sent'] or res['missing']:
        detail['reason'] = 'api_count_mismatch'
    elif journal.get('mismatch'):
        detail['reason'] = 'acta_journal_mismatch'
    return 'reason' not in detail, detail


def _emit(rf, control, result, mclass, detail):
    return Event.objects.create(file=rf, stage=STAGE, control=control, result=result,
                                monitoring_class=mclass, detail=detail,
                                sub_tenant_id=rf.sub_tenant_id)


STRATEGY = 'acta_nav'


def maybe_load(file_id):
    """Chaînage post-identification (orchestrateur webhook + rejeu complet).

    Charge si la Route du fichier porte ``strategy='acta_nav'`` **et** que l'amont
    (admission → identification, stage ``load`` exclu pour qu'un ancien rejet de
    chargement ne bloque pas le rejeu) est ``push``. Ne lève jamais."""
    try:
        rf = ReceivedFile.objects.select_related('route').get(pk=file_id)
        if rf.route is None or rf.route.strategy != STRATEGY:
            return None
        upstream = current_control_rollup([rf.pk], exclude_stages=(STAGE,)).get(rf.pk)
        if not upstream or upstream['monitoring_class'] != Event.MonitoringClass.PUSH:
            return None
        return load_file(rf.pk)
    except Exception:
        logger.exception('Load Acta: échec non bloquant pour file %s', file_id)
        return None


def load_file(file_id, dry_run=False, client=None):
    """Charge un fichier dans Acta. ``dry_run`` → renvoie le plan sans rien écrire.

    Hors dry-run, émet des Event de stage ``load`` (append-only) puis rafraîchit le
    rollup ; Acta injoignable → ``recycle`` (rejouable)."""
    from .admission import _resolve_feed
    rf = ReceivedFile.objects.get(pk=file_id)
    feed = _resolve_feed(rf)
    client = client or ActaClient()
    plan = build_plan(rf, feed, client)
    summary = {
        'funds_total': len(plan.funds),
        'funds_to_create': sum(1 for f in plan.funds.values() if f['uid'] is None),
        'classes_total': len(plan.classes),
        'classes_to_create': sum(1 for c in plan.classes.values() if c['uid'] is None),
        'navs': len(plan.navs), 'errors': plan.errors[:20],
    }
    if dry_run:
        return summary, plan

    with run_scope():
        if plan.errors:
            _emit(rf, CTRL_PLAN, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
                  {'reason': 'invalid_rows', **summary})
            refresh_control_class([rf.pk])
            return summary, None
        try:
            res = apply_plan(client, plan, file_ref=file_ref_of(rf))
        except ActaError as e:
            _emit(rf, CTRL_NAV, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
                  {'reason': 'acta_unreachable', 'error': str(e)[:500]})
            refresh_control_class([rf.pk])
            return summary, None
        ok_ent = not res['failures']
        _emit(rf, CTRL_ENTITIES,
              Event.Result.PASSED if ok_ent else Event.Result.FAILED,
              Event.MonitoringClass.PUSH if ok_ent else Event.MonitoringClass.WARNING_ACTION,
              {'created': len(res['created']), 'links': len(res['links']),
               'failures': res['failures'][:50], 'created_list': res['created']})
        journal = res['journal'] or {}
        ok_nav = not res['nav_rejected']
        detail = {'counts': res['nav_counts'], 'rejected': res['nav_rejected'][:50],
                  'file_ref': file_ref_of(rf)}
        if res['journal'] is not None:
            detail['journal'] = {**journal, 'rejected_lines': journal.get('rejected_lines', [])[:50]}
        _emit(rf, CTRL_NAV,
              Event.Result.PASSED if ok_nav else Event.Result.FAILED,
              Event.MonitoringClass.PUSH if ok_nav else Event.MonitoringClass.WARNING_ACTION,
              detail)
        ok_count, count_detail = count_check(res)
        _emit(rf, CTRL_COUNT,
              Event.Result.PASSED if ok_count else Event.Result.FAILED,
              Event.MonitoringClass.PUSH if ok_count else Event.MonitoringClass.WARNING_ACTION,
              count_detail)
        refresh_control_class([rf.pk])
    logger.info('Load Acta file=%s created=%s links=%s navs=%s rejected=%s',
                rf.pk, len(res['created']), len(res['links']), res['nav_counts'],
                len(res['nav_rejected']))
    return summary, res
