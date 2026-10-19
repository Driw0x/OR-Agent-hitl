# OR-Agent — Mémoire technique du projet

Cette mémoire retrace les réalisations et les décisions techniques d'OR-Agent. Le [README](../README.md) présente le produit et les commandes ; la [roadmap](roadmap.md) conserve les checkpoints réalisés.

## M0 — Cadrage GAP

Le projet retient l'affectation obligatoire et indivisible de chaque tâche à une ressource. Pour une ressource `i` et une tâche `j`, `x[i,j]` est une variable binaire, `c[i,j]` le coût, `a[i,j]` la consommation et `b[i]` la capacité :

```text
Minimiser : somme(i,j) c[i,j] * x[i,j]
Sous contraintes :
    pour chaque tâche j : somme(i) x[i,j] = 1
    pour chaque ressource i : somme(j) a[i,j] * x[i,j] <= b[i]
    x[i,j] appartient à {0, 1}
```

Ce contrat entier permet de construire et de contrôler le modèle sans interprétation supplémentaire dans le solveur. Le [périmètre GAP](../README.md#problème-gap-pris-en-charge) fixe les bornes, les unités et les contraintes admissibles. La validation du modèle, sa fidélité à la source, la faisabilité et l'optimalité constituent des contrôles distincts.

## M1 — Validation et représentation

[models.py](../or_agent/models.py) fournit les contrats communs du CLI, de l'agent et du solveur : `Gap` pour les données, `Extraction` pour les dispositions `ready`, `clarify`, `unsupported` et `invalid`, et `Candidate` pour les affectations et le coût annoncé.

Pydantic impose des modèles stricts et fermés. Les ressources occupent les lignes des matrices et les tâches les colonnes. Les contrôles portent sur les identifiants uniques, les dimensions, les domaines numériques et les unités ; le lecteur JSON refuse aussi les clés dupliquées et les nombres non finis.

Une extraction `ready` exige un problème complet, aucun diagnostic bloquant et une citation exacte de la source pour chacun des sept champs du GAP. Les autres dispositions exigent une explication et ne contiennent pas de problème à résoudre. Les cas valides et les refus sont couverts par [test_models.py](../tests/test_models.py).

## M2 — Résolution CP-SAT

[solver.py](../or_agent/solver.py) construit les variables binaires, l'affectation unique, les capacités et l'objectif. Le LLM fournit des données, jamais du code exécutable au solveur.

CP-SAT utilise un worker, une graine fixe et des tolérances d'optimalité nulles. Le budget par défaut est de cinq secondes. Le résultat conserve candidat, objectif, meilleure borne, version, paramètres et temps du solveur. Le coût du candidat est recalculé en arithmétique entière ; la preuve d'optimalité exige `OPTIMAL` et l'égalité de l'objectif et de la borne.

[test_solver.py](../tests/test_solver.py) compare les petits cas à l'énumération exhaustive et contrôle les statuts, les limites numériques et les budgets. La restitution associe la preuve CP-SAT à la vérification indépendante du candidat.

## M3 — Vérification indépendante

[verify.py](../or_agent/verify.py) utilise un calcul arithmétique séparé, sans importer le solveur ni le convertisseur. Il contrôle les identifiants, la couverture exacte des tâches, les charges, les capacités et le coût total. Les violations sont retournées sous une forme structurée.

[test_verify.py](../tests/test_verify.py) injecte omissions, doublons, identifiants inconnus, dépassements et coûts erronés. Sur l'[exemple fourni](../examples/small.json), le vérificateur retrouve les charges `R1=4`, `R2=2` et le coût `6` ; CP-SAT établit séparément l'optimalité.

## M4 — Boucle agent

[agent.py](../or_agent/agent.py) relie Ollama, décodage, validation, résolution et restitution. La génération a évolué du JSON demandé par instruction vers le schéma structuré et la correction bornée. Les outils d'évaluation conservent les stratégies `json`, `structured` et `structured-repair`.

Le CLI `agent` utilise la génération structurée avec au plus une correction, soit deux appels maximum. Les corrections préservent les données déjà parsées. Les budgets limitent les appels, la durée globale et la résolution ; les observations enregistrent réponses brutes, erreurs, diagnostics et contrôles de ressources.

Les branches sont couvertes par [test_agent.py](../tests/test_agent.py) avec des réponses contrôlées, et un pilote Ollama réel a été exécuté en CLI. Ce parcours résout directement une extraction validée ; l'admission humaine est portée par `route` et le web.

## M5 — Évaluation

Le [corpus initial](../data/benchmark.json) contient **16 cas : 7 de développement et 9 de test**. Le [corpus étendu](../data/extended-benchmark.json) contient **130 cas : 30 de développement et 100 de test**. Ils couvrent instances faisables et infaisables, coûts négatifs, consommations nulles, ambiguïtés et demandes hors périmètre.

Les outils d'évaluation séparent préparation, exécution et relecture des mesures. Les audits contrôlent empreintes, références et séparation des familles. Pour les **105 instances complètes** du corpus étendu, ils vérifient les références, la résolution CP-SAT et la reconstruction depuis la source. Les résultats absents restent comptés dans les dénominateurs.

Les évaluations historiques ont relevé des omissions, associations erronées et transpositions, sans réponse finale correcte récupérée par réparation automatique. Le pilote web a produit six formulations incomplètes et une complète erronée sur sept sources GAP complètes, soit **0/7 formulation complète et fidèle**. Ces observations motivent la revue explicite décrite ci-dessous, sans constituer une mesure générale de fiabilité.

## Pourquoi le HITL a été introduit

La génération d'une formulation structurée ne suffit pas à garantir sa fidélité à l'énoncé. OR-Agent utilise donc une revue humaine explicite avant d'autoriser les résolutions du parcours HITL. Le relecteur compare la source, les données et les contraintes ; son approbation autorise la révision examinée.

## Implémentation du HITL

[hitl.py](../or_agent/hitl.py) définit le format `HITL-GAP: 1` : objectif, inventaires, unités, capacités et un enregistrement par couple tâche–ressource. Une valeur inconnue est représentée par `?` et bloque la complétude ; les lignes `Issue:` portent les diagnostics.

Le parcours conserve la demande originale et enchaîne :

1. Proposition manuelle ou Ollama, puis correction du brouillon.
2. Validation de la syntaxe, de la couverture et des données.
3. Revue de la source, de la proposition et des matrices reconstruites.
4. Confirmation de la portée GAP lorsque le parcours la demande.
5. Approbation explicite de la révision relue.
6. Résolution du GAP approuvé et vérification du candidat.

Les sessions conservent `original.txt`, `proposal.txt`, `session.json`, diagnostics et événements. L'approbation lie les empreintes de la source et de la proposition ; les éléments approuvés sont conservés sous `approved/`. Une modification invalide la revue, la confirmation et l'approbation.

Le CLI utilise les confirmations `SCOPE:<review-token>` et `APPROVE:<review-token>`. Le LLM ne peut pas accorder ces autorisations. [test_hitl.py](../tests/test_hitl.py) vérifie corrections, rejets, inconnues et refus de résolution avec autorisation absente ou périmée.

## Extraction et routage déterministes

[deterministic.py](../or_agent/deterministic.py) extrait des faits associés à un champ, une tâche, une ressource, une valeur, une unité, une citation et des positions dans la source. Les positions suivent les indices de caractères Python, avec intervalle `[start, end)`.

[routing.py](../or_agent/routing.py) reconstruit le GAP et contrôle inventaires, unités, couverture numérique et textuelle, contraintes standard, absence de contradictions et concordance canonique. Le certificat `VERIFIED_COMPLETE` autorise la branche `AUTONOMOUS`, sans LLM ni revue, uniquement pour les formats fermés reconnus. Les autres certificats orientent vers le contrôle humain ou signalent un bloqueur.

Le brouillon manuel reprend les faits individuellement vérifiés et laisse les données manquantes à `?`. La prose non reconnue exige une confirmation explicite du périmètre. Les contradictions et exclusions détectées dans la source persistent après réécriture du brouillon ; corriger la source exige une nouvelle session. [test_routing.py](../tests/test_routing.py) couvre certificats, reconstruction et admission.

## Interface web

[web.py](../or_agent/web.py) réutilise les services de routage, HITL, résolution et vérification. L'interface HTML/CSS/JavaScript présente source, faits, diagnostics, formulation et résultats sans build frontend. Les événements SSE suivent les étapes du traitement.

Les sessions persistent dans `.or-agent-web/` et restent consultables après redémarrage sans relancer les calculs. Les visualisations montrent affectations et capacités ; les exports SVG, HTML et JSON sont liés à la révision courante. Les sessions importées sont autorisées au démarrage et consultées en lecture seule.

Le serveur local mono-utilisateur contrôle l'origine des mutations et l'accès aux fichiers de sessions. La génération facultative vérifie le digest du modèle Ollama et les ressources disponibles ; le brouillon manuel reste accessible sans Ollama.

[test_web.py](../tests/test_web.py) couvre les services HTTP, les autorisations, la suppression et la protection des fichiers. [web_browser.mjs](../tests/web_browser.mjs) exerce les parcours déterministe et HITL dans Chrome, avec CP-SAT et vérificateur réels, génération et approbations contrôlées.

## Bilan des fonctionnalités et validations V1

La V1 réunit contrat GAP strict, extraction et routage déterministes, génération locale facultative, revue HITL, résolution CP-SAT, vérification indépendante, CLI, sessions web, visualisations et exports.

Les validations techniques réalisées comprennent :

- **864 tests réussis, zéro échec et zéro test ignoré** lors de la validation finale sous Windows.
- Parcours Chrome déterministe et HITL : autorisations, invalidation après édition, persistance, visualisations et cohérence des exports JSON/SVG/HTML, aux largeurs **1440, 1280 et 390 px**, avec actions mobiles.
- Installations Windows sous Python 3.13 et Linux Ubuntu WSL2 sous Python 3.12, avec dépendances figées, installation éditable, exemples et audits hors ligne. Les parcours Chrome ont été validés sous Windows.
- Imports, compilation des sources, cohérence des dépendances, commandes CLI et audits des deux corpus.

Les contrôles historiques après reconstruction des propositions par l'investigateur et approbation humaine ont obtenu six optima **15, 2, −7, 5, 55 et 56**, avec candidats vérifiés, et une infaisabilité conforme à l'énumération. Les 21 contrôles d'exports JSON/SVG/HTML associés ont réussi. Les trois contrôles déterministes distincts ont donné les optima **6 et −5**, vérifiés, et une infaisabilité conforme.

**V1 terminée sur son périmètre fonctionnel et technique.**
