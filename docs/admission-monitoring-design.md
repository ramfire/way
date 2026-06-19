# AlfaWay — Admission & Monitoring : note de design (2026-06-16)

> Note de cadrage destinée à la base de connaissance projet. Fait suite au constat
> opérationnel : *« les fichiers au verdict `archive` restent dans l'écran Live »*.
> Ce constat n'est **pas un bug d'affichage** — il révèle un besoin de modèle, en
> particulier parce que **le nombre de contrôles va croître** (l'admission n'est
> que le premier maillon d'une chaîne ; les contrôles DORA suivront).
>
> Complète `docs/AlfaWay-technical-overview.md`. Décrit une **cible de design**,
> pas l'état déployé. Décisions à valider avant implémentation.

---

## 1. Le déclencheur, en une phrase

Un fichier classé `archive` par l'admission **reste visible en Live** parce qu'il
est `stored`. C'est cohérent avec l'invariant *« l'admission ne déplace jamais un
fichier »*, mais ça montre que **le résultat des contrôles n'a aucun exutoire
structuré dans l'UI** : il n'est qu'un badge passif.

## 2. Constat de fond : deux axes orthogonaux

La vie d'un fichier se décrit selon **deux dimensions indépendantes** :

| | **Axe A — stockage** (`ReceivedFile.state`) | **Axe B — contrôles** (`Event`) |
|---|---|---|
| Question | *où sont les octets ? l'objet existe-t-il ?* | *que vaut ce flux au regard des règles ?* |
| Valeurs | `receiving / stored / failed / deleted / missing / archived` | résultat de N contrôles → classe de monitoring |
| Nature | technique / S3 | métier / réglementaire |
| Pilote Live/History | **Oui** | **Non (aujourd'hui)** |

Ils sont **réellement décorrélés** : `stored`+`archive`, `stored`+`admis`,
`deleted`+`admis`… coexistent. Les onglets Live/History ne filtrent que l'axe A.

## 3. Décision de cadrage retenue — **Option 1 : axes séparés**

> Live/History **restent l'axe A** (stockage). On ajoute une **lecture dédiée à
> l'axe B**, sans jamais fusionner les deux ni violer l'invariant « l'admission ne
> touche pas `state` ».

Écartée : *Option 2 (reframer Live/History par « actionabilité »)* — fusionne les
deux axes, impose un modèle de triage mutable, plus de risque. À reconsidérer plus
tard si un vrai workflow ops est requis (voir §9).

## 4. Hypothèse structurante : **N contrôles, M stages**

`Event` est **déjà** conçu comme la colonne vertébrale de toutes les étapes
(`stage`), chacune émettant plusieurs `control`. L'admission expose aujourd'hui
4 contrôles (`partner_recognised`, `partner_status`, `channel_authorised`,
`verdict`) ; demain, `qualification`, `routage`, `parsing`, contrôles DORA…
ajouteront des dizaines de contrôles sur d'autres stages.

> **Conséquence n°1 (principe directeur).** L'UI de l'axe B doit être **générique
> sur `(stage, control, monitoring_class)`**. On ne câble **jamais** un nom de
> contrôle ni un stage dans le board/feed. Ajouter un contrôle DORA demain ne doit
> demander **aucune** retouche UI.

La colonne actuelle « Admission » (badge `verdict`) est un **cas particulier**
admission-only : elle ne passe pas à l'échelle. La modale détail, elle, est déjà
générique (elle déroule les events tels quels) — on la garde.

## 5. Le bon levier : `monitoring_class`, pas le nom du contrôle

`monitoring_class` est l'abstraction qui **découple le comportement du board du
contenu métier** du contrôle. Six classes, sémantique = comportement attendu :

| Classe | Sens | Action ops | Visibilité board (proposé) |
|---|---|---|---|
| `blocking` | arrête la chaîne | corriger d'urgence | en tête « à traiter » |
| `warning_action` | anomalie, action requise | traiter | « à traiter » |
| `recycle` | retraitable après correction (humain) | enrôler/corriger puis rejouer | backlog « à traiter » |
| `reject` | terminal, gardé pour audit, non retraité | revue/audit | « terminé / audit » |
| `warning_noaction` | informatif | — | discret |
| `push` | confirmation, tout va bien | — | discret / vert |

> **Conséquence n°2 (read-model).** Le statut « axe B » courant d'un fichier =
> **agrégat des derniers events par `(stage, control)`**, réduit à **la classe la
> plus sévère** (règle *worst-wins*). Un seul signal par fichier, quel que soit le
> nombre de contrôles. Échelle de sévérité **DÉCIDÉE (2026-06-16, board orienté
> action — l'actionnable prime sur le terminal)** :
> `blocking > warning_action > recycle > reject > warning_noaction > push`.
> Rationale : `recycle` (intervention humaine requise) remonte **avant** `reject`
> (terminal, classé sans suite).
>
> (Variante possible : un rollup **par stage** plutôt que global, pour une vue
> « pipeline » — voir §6.)

## 6. Conséquences UI concrètes (cible)

1. **Colonne générique « Contrôles » (DÉCIDÉ — Option A, rollup *worst-wins*)** à
   la place de la colonne « Admission » hardcodée : **un seul badge** par fichier,
   piloté par la classe la plus sévère, tous stages/contrôles confondus. Scale à
   l'infini (N contrôles → toujours 1 colonne) ; le détail par stage/contrôle reste
   dans la modale. *Écartée : la vue « pipeline » (une colonne par stage) — non
   tenable au-delà de quelques stages. Pourra être ré-introduite comme « déplier »
   optionnel si besoin, sans remettre en cause le rollup par défaut.*
2. **Filtre par classe de monitoring** (chips, comme les chips d'état) : isoler/
   masquer `recycle`, `reject`, etc. — c'est la réponse directe à *« je ne veux
   plus voir les archive dans mon écran »*, **sans toucher au `state`**.
3. **Agrégation par cause** (voir §7) — la fonctionnalité à plus forte valeur.
4. **Modale détail** : déjà générique (stage/control/result/classe/détail/heure) —
   inchangée ; elle absorbera naturellement les contrôles futurs.

## 7. Agréger **par cause** — en complément (unité de travail = le fichier)

> **Unité de travail DÉCIDÉE (2026-06-16) : le FICHIER.** Le board ligne-par-ligne
> reste la **vue principale** de triage ; on agit fichier par fichier. La vue
> agrégée par cause ci-dessous est un **complément** (lecture/insight), **pas** la
> vue de triage primaire.

Exemple réel : 2092 fichiers `quarantine` proviennent **d'une seule condition**
(*partenaire `''` révoqué qui émet encore*). Même si l'action s'effectue au niveau
fichier, il reste utile de **voir** que 2092 lignes partagent une cause.

> **Conséquence n°3 (complément).** Prévoir une **vue agrégée** « par cause » =
> `(stage, control, result, detail.reason [, username/partner])` → compte +
> exemples, en **second niveau** (onglet/section annexe, pas le board principal).
> Elle sert d'**insight** (« ces 2092 partagent une cause ») et fait **remonter les
> signaux** aujourd'hui enterrés (le `warning_action` *revoked_partner_still_emitting*
> est un signal sécurité/ops, pas du bruit). Le travail, lui, se fait sur le board
> fichier (filtre par classe, §6.2).

## 8. Dette à corriger quoi qu'il arrive : la collision « archive »

Le mot porte **deux sens incompatibles** :

| | `State.ARCHIVED` (axe A) | `monitoring_class=reject` / verdict `archive` (axe B) |
|---|---|---|
| posé par | bouton manuel, sur un `failed` | un contrôle, automatiquement |
| sens | « échec traité, sorti de History » | « flux non reconnu, gardé pour audit » |

→ **Désambiguïser par renommage. DÉCIDÉ (2026-06-16) : le verdict `archive`
devient `quarantine`.** Sens : *flux non reconnu, mis de côté pour audit, non
retraité* — « quarantine » le décrit mieux qu'« archive ». `State.ARCHIVED`
(axe stockage, bouton manuel, onglet History) **ne bouge pas** → on garde le code
éprouvé, et « archive » reste sans ambiguïté côté stockage.

Portée du renommage :
- `api/admission.py` — `VERDICT_ARCHIVE = 'quarantine'` (+ docstrings/logs).
- `detail.verdict` **déjà émis** en base = `archive` → réémis en `quarantine` en
  **rejouant l'admission** (append-only, idempotent, déjà outillé).
- i18n board : libellés `adm_archive` → clé/texte `quarantine` (EN/FR/IT), classe
  CSS `adm-archive` → `adm-quarantine` (couleur rouge inchangée).
- `monitoring_class` **inchangée** (`reject`) : c'est la classe, pas le verdict.

## 9. Invariants à préserver (ne pas casser)

1. **L'admission (et tout contrôle axe B) ne modifie jamais `state`.** Axe B ≠ axe A.
2. **`Event` est append-only** : un statut « courant » se *dérive* (dernier event),
   il ne s'écrase pas. Un éventuel statut de triage mutable (traité/en attente)
   serait un **read-model séparé**, pas une mutation d'`Event`.
3. **Les contrôles ne bloquent pas l'upload** : observation post-stockage, la
   réponse webhook reste 200 (cf. `SFTPWebhookView`).
4. **Générique sur `(stage, control, monitoring_class)`** : aucun nom de contrôle
   en dur côté feed/UI.

## 10. Plan par étapes (par valeur croissante de design / risque)

1. ✅ **FAIT (commit `4393e87`)** — **Désambiguïser « archive »** : verdict
   `archive` → `quarantine` (§8). Events réémis par rejeu.
2. ✅ **FAIT (commit `24dcc29`)** — **Feed générique axe B** : `current_control_rollup`
   expose par fichier le rollup *worst-wins* (`control_class` + stage/contrôle
   d'origine), générique sur tous les contrôles. Surface le `warning_action`
   enterré. *UI board encore câblée sur le verdict (basculée à l'étape 3).*
3. ✅ **FAIT (commit à venir)** — colonne générique **« Contrôles »** (`control_class`,
   badge par classe, **tri par sévérité**) + **chips de filtre par classe** côté
   serveur (`?control=`, comptes `per_control_class`). Read-model **matérialisé** :
   champ `ReceivedFile.control_class` (worst-wins), rafraîchi par
   `refresh_control_class()` à chaque passage d'admission → filtre/tri/agrégation en
   SQL indexé sur TOUTE la table (pas de recalcul par poll). Contrat : tout futur
   émetteur de contrôle (DORA…) doit appeler `refresh_control_class` après émission.
4. ✅ **FAIT (commit à venir)** — **Vue agrégée par cause** (§7) : endpoint
   `monitoring_causes` (groupe l'état courant des contrôles en échec par
   `(stage, control, classe, raison)` → nb fichiers + partenaires + exemples, trié
   par sévérité) + bouton « ⚠ Causes (N) » → modale. **Complément** (second niveau,
   lecture seule) ; l'unité de travail reste le fichier. Transforme « 2092 lignes »
   en « 1 cause » et surface le signal `revoked_partner_still_emitting`.
5. ✅ **FAIT (commit à venir)** — modèle de **triage mutable** « les deux » niveaux
   (décision 2026-06-16) : `TriageAck` (par **cause**, claim/resolve/reopen + owner +
   note) et `FileTriage` (override par **fichier**, sparse). Mutables, **distincts**
   du journal append-only. **Règle de réconciliation** : un contrôle en échec est « à
   traiter » sauf si son fichier a un override `resolved` **OU** sa cause un ack
   `resolved` ; l'override fichier prime. `files_open` = fichiers à traiter après
   réconciliation (un fichier reste ouvert tant qu'**une** de ses causes non résolues
   le couvre). UI : workflow dans la modale Causes + action par ligne sur le board.
   **Fichiers traités masqués par défaut** sur le board (toggle « Masquer traités » /
   `?show_handled=1`) → le board ne montre que ce qui reste à faire.

## 11. Questions ouvertes à trancher

- ~~**Rollup global *worst-wins* vs vue « pipeline » par stage**~~ — **TRANCHÉ
  (2026-06-16) : Option A**, rollup global *worst-wins*, une seule colonne (§6.1).
- ~~**Échelle de sévérité** exacte des classes~~ — **TRANCHÉ (2026-06-16) :**
  `blocking > warning_action > recycle > reject > warning_noaction > push`
  (l'actionnable `recycle` prime sur le terminal `reject`) (§5).
- ~~**Renommage** : quel côté de « archive » bouge~~ — **TRANCHÉ (2026-06-16) :**
  le **verdict** `archive` → `quarantine` ; `State.ARCHIVED` inchangé (§8).
- ~~**Unité de travail** opérateur : le fichier, ou la cause/partenaire~~ —
  **TRANCHÉ (2026-06-16) : le FICHIER.** Board fichier = vue principale ; agrégat
  par cause = complément de second niveau (§7).

## 12. Qualification (§1.3) — spec (2026-06-19)

> Deuxième producteur de l'axe B après l'admission. **Observation/classification
> pure**, même contrat que l'admission (§9) : append-only, rejouable, ne touche
> jamais `state`, ne lève jamais. Le board reste **générique** (§4/§5) — la
> qualification n'ajoute qu'un `stage` et des `control`, aucun recâblage UI.

**Modèle (déjà migré, 7 entités).** `Nomenclature(channel, sub_tenant, subfolder,
grammar JSON, mandatory JSON list, active)`, `UniqueConstraint(channel, subfolder)`.
`Event.stage` accepte `qualification` ; `Event.cause_code` porte la cause normalisée.

**Sélecteur.** `subfolder = dirname(s3_key)` (le dossier SFTP, dépouillé des `/` de
bord) ; le **nom de fichier** = `basename(s3_key)`.

**Grammaire.** `grammar = {"filename": "<regex>"}`. Match = `re.fullmatch(regex,
basename)`. `grammar` sans clé `filename` ⇒ **aucune contrainte de nom** (on ne
bloque pas par défaut). Regex invalide ⇒ erreur de **config** → `recycle`
(`cause_code=grammar_invalid`), corrigeable puis rejeu.

**`mandatory` : différé.** Concept de complétude d'un *dossier* (N fichiers
attendus) — incompatible avec la granularité *par fichier* retenue ici. Colonne
conservée, **non câblée** dans cet incrément (futur stage « complétude/batch »).

**Déclenchement.** Chaînée dans `file_admission`, **uniquement** après un verdict
admission `admis` (donc canal/partenaire résolus). Rejouable : le rejeu d'admission
(modale « Rejouer », action « Handle ») ré-exécute admission **puis** qualification.
Un seul `refresh_control_class` en fin de chaîne couvre les deux stages.

**Contrôles → verdicts (par fichier).**

| Contrôle | Condition d'échec | Verdict | `monitoring_class` | `cause_code` |
|---|---|---|---|---|
| `nomenclature_recognised` | pas de `Nomenclature(channel, subfolder, active)` | **recycle** | `recycle` | `nomenclature_not_found` |
| `filename_grammar` | `basename` ne matche pas la regex | **quarantine** | `reject` | `filename_grammar_mismatch` |
| `filename_grammar` | regex de config invalide | **recycle** | `recycle` | `grammar_invalid` |
| `verdict` | tout passe | **qualified** | `push` | — |

> **Discovery (recycle) vs non-conforme (quarantine).** Pas de nomenclature = trou
> d'enrôlement, **retraitable** : un humain crée la `Nomenclature` puis rejoue
> (miroir de `partner_not_mapped`). Nom non conforme = le fichier lui-même est
> mauvais, **non retraité** : gardé pour audit (miroir de `partner_revoked`).

**Board.** `current_control_rollup` (worst-wins, tous stages) gère seul la
combinaison admission×qualification : un fichier `admis`+`quarantine` remonte en
`reject`, `admis`+`recycle` en `recycle`, `admis`+`qualified` en `push`. « Handle »
ne pose le flag « traité » que si le rollup redevient `push` (donc admis **et**
qualifié) — inchangé.
