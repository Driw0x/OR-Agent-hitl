# OR-Agent

Application locale pour structurer, résoudre et vérifier des problèmes d'affectation sous capacités.

## Présentation

OR-Agent transforme les données d'un **problème d'affectation généralisée (GAP, Generalized Assignment Problem)** en une formulation structurée, valide cette formulation, puis la résout avec CP-SAT. Un vérificateur indépendant recalcule les affectations, les charges et le coût.

L'interface web et le CLI proposent une extraction déterministe, une génération locale facultative avec Ollama et un parcours de revue et d'approbation humaine (HITL). Les sessions, visualisations et exports permettent de consulter et de conserver les résultats.

**V1 terminée sur son périmètre fonctionnel et technique.**

## Problème GAP pris en charge

Le GAP retenu repose sur les règles suivantes :

- Toutes les tâches sont obligatoires et indivisibles.
- Chaque tâche est affectée exactement une fois à une ressource.
- La consommation d'une tâche peut dépendre de la ressource choisie.
- Pour chaque ressource, la somme des consommations ne dépasse pas sa capacité.
- L'objectif est de minimiser la somme des coûts d'affectation.

Les coûts sont des entiers entre `-10^9` et `10^9`. Les consommations et capacités sont des entiers entre `0` et `10^9`. Les coûts négatifs et les consommations nulles sont autorisés. Les unités doivent être explicites ; aucune conversion implicite n'est effectuée. Le modèle accepte au plus **10 000 couples tâche–ressource**.

Routage, précédences, échéances, ordonnancement temporel, tâches facultatives et contraintes supplémentaires entre tâches sortent de ce contrat. Une demande incomplète appelle une clarification ; une contrainte hors périmètre ne doit pas être supprimée pour rendre le problème soluble.

## Fonctionnement

```text
Énoncé original
    |
    v
Extraction déterministe des faits et contrôle du périmètre
    |                                              |
    v                                              v
Format fermé certifié                    Entrée non certifiée
Construction directe du GAP              Brouillon manuel ou Ollama
Sans LLM ni revue humaine                Correction et validation
    |                                    Revue et confirmation de portée
    |                                    Approbation de la révision exacte
    |                                              |
    +----------------------+-----------------------+
                           |
                           v
                    Conversion CP-SAT
                           |
                           v
                    Résolution sous budget
                           |
                           v
                    Vérification arithmétique indépendante
                           |
                           v
                    Résultat, visualisations et exports
```

La certification déterministe concerne uniquement les formats fermés reconnus, avec contrôle des données et de leur provenance. Pour les autres entrées, une proposition manuelle ou Ollama doit être contrôlée, revue et approuvée. L'approbation autorise la révision relue ; elle ne garantit pas automatiquement sa fidélité à l'énoncé. Toute modification invalide les autorisations associées.

Un modèle validé respecte le contrat de données. Un candidat vérifié est faisable pour le GAP fourni. L'optimum exige en plus une preuve CP-SAT :

- `OPTIMAL` : optimum établi lorsque la preuve et la vérification du candidat sont valides.
- `FEASIBLE` : candidat disponible, soumis au vérificateur ; optimalité non prouvée.
- `INFEASIBLE` : GAP fourni prouvé infaisable.
- `UNKNOWN` : recherche inconclusive dans le budget.

Une erreur technique est signalée séparément.

## Fonctionnalités

### Extraction et formulation

- Modèles Pydantic stricts : types, dimensions, identifiants uniques, unités et refus des champs inconnus.
- Provenance des données et diagnostics sur les éléments manquants, contradictoires ou non interprétés.
- Reconstruction déterministe pour les formats fermés reconnus.
- Édition d'une proposition lisible, avec contrôle des associations tâche–ressource et des matrices reconstruites.
- Revue humaine, confirmation de la portée GAP et approbation liée aux empreintes de la révision.

### Résolution et vérification

- Variables binaires d'affectation, capacités cumulatives et objectif de coût construits par le code.
- Résolution CP-SAT avec limite de temps, candidat et borne disponibles selon le statut.
- Vérification indépendante de la couverture des tâches, des charges et du coût total.
- Restitution distinguant solution faisable, optimum prouvé, infaisabilité et absence de conclusion.

### Interface et traçabilité

- Interface web locale, sans build frontend, avec suivi des étapes par événements serveur (SSE).
- Sessions persistées, consultation après redémarrage et diagnostics de génération.
- Visualisation des affectations et des capacités utilisées ; exports SVG, HTML et JSON.
- Import en lecture seule de sessions existantes explicitement autorisées au démarrage.

Les outils d'évaluation auditent les corpus et comparent les méthodes de génération et de correction.

## Architecture et technologies

```text
or_agent/                      Cœur du projet : extraction, validation, revue humaine, résolution et interface web
data/                          Jeux de données et références d'évaluation
examples/                      Exemples de problèmes GAP
tests/                         Tests automatisés
docs/                          Roadmap et mémoire technique
```

Le socle utilise **Python**, **Pydantic**, **OR-Tools CP-SAT** et **pytest**. Ollama fournit la génération locale facultative avec `qwen2.5-coder:7b`. Le serveur HTTP et les appels Ollama utilisent la bibliothèque standard Python ; le frontend utilise HTML, CSS et JavaScript.

## Installation

Prérequis : **Python 3.12 minimum ; installations vérifiées sur Python 3.12 et 3.13**, Git ; Ollama facultatif. Les versions de l'environnement reproductible sont figées dans [requirements.txt](requirements.txt). Les dépendances du paquet et la même version Python minimale sont déclarées dans [pyproject.toml](pyproject.toml).

Sous Windows PowerShell, cloner le dépôt. Pour une copie existante, commencer à la création de l'environnement depuis sa racine.

```powershell
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt
& $py -m pip install --no-deps -e .
& $py -m pip check
```

Sous Linux :

```sh
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
```

`python3` doit correspondre à Python 3.12 minimum. S'il pointe vers une version plus ancienne, utiliser explicitement un interpréteur compatible installé, par exemple `python3.12 -m venv .venv`. Les dépendances figées exigent également Python 3.12 minimum.

L'installation en mode éditable fournit aussi la commande `or-agent`. Les exemples ci-dessous utilisent `python -m or_agent` pour identifier explicitement l'environnement Python.

## Utilisation de l'interface web

Démarrer l'interface depuis la racine, sous PowerShell :

```powershell
$py = ".\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py -B -m or_agent web
```

Sous Linux : `.venv/bin/python -B -m or_agent web`.

Ouvrir [http://127.0.0.1:8765](http://127.0.0.1:8765), avec cette adresse exacte plutôt que `localhost`. Le serveur est local et mono-utilisateur. Les sessions persistent dans `.or-agent-web/`. **Ctrl+C** attend la fin des traitements actifs avant l'arrêt, sans arrêter un serveur Ollama partagé.

1. Saisir un problème GAP ou choisir un exemple intégré, puis cliquer **Lancer l'analyse**.
2. Consulter les faits extraits et les diagnostics. Une entrée entièrement certifiée est résolue directement sans LLM ni revue humaine.
3. Sinon, saisir une proposition ou cliquer **Demander une proposition Ollama**. Comparer à la source, corriger, **Enregistrer les modifications**, puis **Vérifier la proposition**.
4. **Effectuer la revue**, puis **Confirmer la portée**. Cocher l'approbation et cliquer **Résoudre le GAP approuvé** pour approuver et résoudre cette révision exacte.
5. Consulter le statut, le coût, les charges et la vérification indépendante.
6. Examiner les visualisations et exporter les résultats. Une modification invalide les approbations et les exports courants.

Les données manquantes, contradictions et contraintes hors périmètre détectées bloquent l'approbation. Une contradiction de la source exige un énoncé corrigé dans une nouvelle session : effacer son diagnostic du brouillon ne la résout pas. Le parcours manuel et les exemples certifiés fonctionnent sans Ollama.

Les options `--port`, `--sessions`, `--base-url` et `--import-session` permettent d'adapter le lancement. Consulter `& $py -m or_agent web --help` ; sous Linux, remplacer `& $py` par `.venv/bin/python`.

### Exemple sans LLM

Cet énoncé suit le format fermé à lignes étiquetées. Il peut être collé dans l'interface :

```text
Assign each task exactly once; minimize total cost within capacities.
Tasks: T1, T2, T3.
Resources: R1, R2.
Cost unit: credit.
Resource unit: slot.
R1 capacity: 4.
R1 costs: T1=2, T2=8, T3=3.
R1 consumption: T1=2, T2=2, T3=2.
R2 capacity: 2.
R2 costs: T1=5, T2=1, T3=4.
R2 consumption: T1=2, T2=2, T3=2.
```

L'affectation optimale est `T1 → R1`, `T2 → R2`, `T3 → R1`. Le coût est **6 credits**, avec une charge de **4 slots sur R1** et **2 slots sur R2**. La même instance est fournie en JSON dans [examples/small.json](examples/small.json).

Les mots-clés, la ponctuation et les associations explicites appartiennent au format reconnu. Une traduction libre ou une reformulation peut basculer vers la revue humaine. [examples/small.txt](examples/small.txt) est une version en prose destinée au parcours de génération ; ce n'est pas le format certifié ci-dessus.

## Utilisation du CLI

### Valider, résoudre et vérifier un GAP JSON

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m or_agent validate examples/small.json
& $py -B -m or_agent solve examples/small.json --seconds 5
& $py -B -m or_agent verify examples/small.json examples/candidate.json
```

Les matrices JSON utilisent **les ressources en lignes et les tâches en colonnes**. `validate` contrôle le contrat structurel. `solve` résout ce modèle et vérifie son candidat. `verify` recalcule la faisabilité et le coût d'un candidat fourni, sans rechercher d'optimum. Ces commandes travaillent directement sur le GAP JSON.

[examples/infeasible.json](examples/infeasible.json) illustre une instance infaisable ; [examples/multiple.json](examples/multiple.json) contient une instance à optima multiples. `schema` expose le schéma de sortie attendu pour l'extraction.

### Analyser un énoncé avant admission

```powershell
& $py -B -m or_agent route analyze examples/small.txt
```

Cette commande affiche les faits déterministes et le certificat, sans appeler Ollama ni résoudre. `route submit` crée ensuite une session avec branche certifiée ou HITL. Les sous-commandes `review`, `validate`, `scope`, `approve`, `reject`, `status` et `solve` exposent les étapes de revue. Consulter `& $py -m or_agent route --help` et la [mémoire technique](docs/memory.md) pour les contrats de révision.

### Génération et correction bornée

```powershell
& $py -B -m or_agent agent examples/small.txt --total-seconds 180 --solver-seconds 5
```

La génération structurée permet au plus une correction après diagnostic, soit deux appels maximum. `--max-calls 1` limite le traitement à un appel. La commande `agent` résout directement une extraction validée, sans approbation humaine. Pour contrôler la proposition avant résolution, utiliser le web ou `route`.

## Configuration Ollama facultative

Ollama est facultatif. Après son installation séparée, préparer le modèle :

```powershell
ollama pull qwen2.5-coder:7b
ollama list
```

Le web utilise `http://127.0.0.1:11434` par défaut ; `--base-url` permet de choisir une autre adresse. Il contrôle le digest attendu du modèle dans [web.py](or_agent/web.py) : un nom identique avec un digest différent peut être refusé. Le CLI `agent` accepte aussi `--model` ; cela ne change pas le modèle autorisé par le web.

Les contrôles de ressources peuvent refuser la génération ou interrompre l'attente côté client. Un timeout client ne garantit pas l'annulation du calcul dans Ollama. Les commandes `resources` et `ollama` exposent les diagnostics disponibles ; la saisie manuelle reste utilisable si la génération est indisponible.

## Tests

Depuis la racine, sans lancer de campagne Ollama :

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m pytest -q -ra
& $py -B -m or_agent.evaluate audit
& $py -B -m or_agent.extended_corpus audit
```

Sous Linux, remplacer `& $py` par `.venv/bin/python`.

Les tests couvrent les schémas, la comparaison CP-SAT/énumération exhaustive, les candidats corrompus, les budgets, la provenance, les évaluations et les sessions HITL/web. Les tests de génération utilisent des réponses contrôlées.

La suite complète exige **Node 22 minimum et Chrome/Chromium** pour piloter un véritable navigateur via son protocole de débogage, sans dépendance JavaScript supplémentaire. `OR_AGENT_CHROME` permet de fournir le chemin du navigateur s'il n'est pas détecté. L'absence d'outils produit un échec explicite. Les tests de protections de fichiers exigent les permissions de création de liens.

Pour exécuter uniquement les parcours navigateur, déterministe et HITL, avec contrôle des exports et des largeurs 1440, 1280 et 390 px :

```powershell
& $py -B -m pytest -q -s tests/test_web.py::test_browser_session_deletion_and_combined_approval
```

Le navigateur utilise le serveur HTTP local et le vrai solveur CP-SAT. L'indisponibilité d'Ollama et une réponse de génération sont contrôlées ; aucune génération LLM réelle n'est requise.

Les corpus et leurs références permettent des audits hors ligne sans Ollama. Le [bilan V1](docs/memory.md#bilan-des-fonctionnalités-et-validations-v1) résume les validations techniques réalisées.

## Documentation

- [Roadmap](docs/roadmap.md) : milestones, checkpoints réalisés et statuts V1.
- [Mémoire technique](docs/memory.md) : réalisations M0–M5, HITL, routage déterministe, interface web et validations V1.
- [Exemples](examples/small.json) : point de départ pour vérifier la résolution sans LLM.

### Licence

Ce projet est distribué sous licence MIT. Voir [LICENSE](LICENSE).

Copyright © 2026 Victor YE.
