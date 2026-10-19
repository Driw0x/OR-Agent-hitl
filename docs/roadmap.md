# OR-Agent — Roadmap et checkpoints de validation

Cette roadmap retrace les milestones et les checkpoints réalisés. Le [README](../README.md) présente le produit et son utilisation ; la [mémoire technique](memory.md) explique les choix d'implémentation.

## M0 — Cadrage GAP

Objectif : définir le contrat d'affectation sous capacités.

- [x] Fixer l'affectation obligatoire, indivisible et unique de chaque tâche.
- [x] Définir les coûts et consommations par couple tâche–ressource, les capacités et la minimisation du coût total.
- [x] Fixer les bornes entières et les unités explicites, avec coûts négatifs et consommations nulles.
- [x] Délimiter le GAP par rapport au routage, aux précédences et à l'ordonnancement.
- [x] Distinguer validation du modèle, fidélité à la source, faisabilité et optimalité.

Validation : contrat implémenté dans [models.py](../or_agent/models.py), couvert par les tests de schéma et de résolution. **Statut : terminé.**

## M1 — Validation et représentation

Objectif : représenter les données dans des modèles stricts et contrôlables.

- [x] Implémenter les modèles fermés `Gap`, `Extraction` et `Candidate`.
- [x] Contrôler identifiants, unicité, dimensions, domaine numérique et unités.
- [x] Fixer les ressources en lignes et les tâches en colonnes dans les matrices.
- [x] Exiger une provenance exacte pour chaque champ d'une extraction `ready`.
- [x] Refuser les JSON ambigus, les données incomplètes et les champs inconnus.

Validation : cas valides et refus couverts par [test_models.py](../tests/test_models.py). **Statut : terminé pour la validation structurelle.**

## M2 — Résolution CP-SAT

Objectif : convertir et résoudre le GAP fourni sous une limite de temps.

- [x] Construire les variables binaires, l'affectation unique, les capacités et l'objectif entier.
- [x] Conserver candidat, objectif, borne, version, paramètres et temps du solveur.
- [x] Distinguer optimum prouvé, candidat faisable, infaisabilité, résultat inconclusif et erreur technique.
- [x] Comparer de petits problèmes à l'énumération exhaustive et tester les limites numériques et temporelles.

Validation : conversion, statuts et références exhaustives couverts par [test_solver.py](../tests/test_solver.py). **Statut : terminé pour le GAP fourni.**

## M3 — Vérification indépendante

Objectif : contrôler les affectations par un calcul séparé du solveur.

- [x] Contrôler les identifiants, la couverture exacte des tâches et les doublons.
- [x] Recalculer les charges entières et vérifier les capacités.
- [x] Recalculer le coût et le comparer à l'objectif annoncé.
- [x] Retourner des violations structurées et refuser les candidats mal formés.
- [x] Tester omissions, doublons, ressources inconnues, dépassements et coûts corrompus.

Validation : corruptions injectées couvertes par [test_verify.py](../tests/test_verify.py). **Statut : terminé pour la faisabilité et le coût.**

## M4 — Boucle agent

Objectif : relier Ollama, validation et résolution dans une boucle bornée.

- [x] Livrer le CLI de génération et les diagnostics structurés.
- [x] Implémenter et comparer JSON par prompt, génération par schéma et correction bornée.
- [x] Limiter la génération à un appel, ou deux avec correction, et appliquer les budgets.
- [x] Préserver les données déjà parsées lors d'une correction.
- [x] Enregistrer réponses brutes, erreurs, observations et contrôles de ressources.
- [x] Tester les branches avec des réponses contrôlées et exécuter un pilote Ollama réel en CLI.

Validation : enchaînements couverts par [test_agent.py](../tests/test_agent.py). **Statut : terminé pour la boucle CLI sans admission HITL.**

## M5 — Évaluation

Objectif : évaluer les formulations et les réponses finales à partir de références indépendantes.

- [x] Préparer le corpus initial de 16 cas : 7 de développement et 9 de test.
- [x] Préparer le corpus étendu de 130 cas : 30 de développement et 100 de test.
- [x] Auditer références, empreintes et séparation des familles entre développement et test.
- [x] Implémenter les campagnes journalisées et les métriques, en comptant les résultats absents dans les dénominateurs.
- [x] Réaliser les évaluations historiques initiale, étendue et indépendante.
- [x] Analyser les omissions, associations erronées, transpositions et corrections.

Validation : corpus couverts par [test_benchmark_data.py](../tests/test_benchmark_data.py) et [test_extended_corpus.py](../tests/test_extended_corpus.py), protocoles et métriques par les tests d'évaluation. **Statut : évaluation exploratoire réalisée.** Les résultats utiles aux décisions sont résumés dans la [mémoire](memory.md#m5--évaluation).

## Extensions — Routage déterministe et HITL

Objectif : résoudre directement les formats certifiés et soumettre les autres formulations à une revue explicite.

- [x] Extraire les faits avec associations, unités, citations et positions dans la source.
- [x] Certifier couverture, contraintes standard, absence de contradictions et reconstruction canonique.
- [x] Construire et résoudre les entrées `VERIFIED_COMPLETE` sans LLM.
- [x] Produire sinon un brouillon manuel ou une proposition Ollama facultative, avec inconnues et diagnostics.
- [x] Ajouter correction, validation, revue, confirmation de portée, approbation et rejet.
- [x] Lier les autorisations à la source et à la proposition exactes, puis les invalider après modification.
- [x] Maintenir les bloqueurs de la source originale après réécriture du brouillon.
- [x] Vérifier les gardes avec des approbations simulées.

Validation : révisions, autorisations et routage couverts par [test_hitl.py](../tests/test_hitl.py) et [test_routing.py](../tests/test_routing.py). **Statut : terminé sur le périmètre technique V1.**

## Extension — Interface web

Objectif : réunir saisie, revue, résolution et restitution dans une interface locale.

- [x] Livrer le serveur local mono-utilisateur et l'interface sans build frontend.
- [x] Afficher les étapes et diagnostics par événements SSE.
- [x] Intégrer brouillon manuel, Ollama, édition, validation, revue et approbation.
- [x] Persister les sessions et les consulter après redémarrage sans relancer les traitements.
- [x] Ajouter visualisations, exports SVG/HTML/JSON et invalidation après édition.
- [x] Autoriser explicitement les imports de sessions en lecture seule.
- [x] Tester contrôles HTTP, suppression et protections des fichiers de sessions.
- [x] Exécuter sous Chrome le format certifié du README, avec optimum 6 et candidat vérifié.
- [x] Exécuter le HITL manuel, le refus sans approbation, l'invalidation et la nouvelle approbation.
- [x] Vérifier persistance, diagnostics, visualisations et cohérence des exports.
- [x] Contrôler les largeurs 1440, 1280 et 390 px, avec actions mobiles et sans débordement global ni erreur JavaScript bloquante.
- [x] Vérifier le parcours sans Ollama et une réponse de génération contrôlée.

Validation : services couverts par [test_web.py](../tests/test_web.py), parcours Chrome par [web_browser.mjs](../tests/web_browser.mjs), avec génération et approbations contrôlées. **Statut : terminé sur le périmètre fonctionnel et technique web V1.**

## Clôture V1

Objectif : vérifier les parcours intégrés et consolider les observations réalisées.

- [x] Exécuter un pilote Ollama réel depuis le web avec des références indépendantes.
- [x] Réaliser des revues humaines et comparer les formulations initiales et approuvées.
- [x] Contrôler résolution, vérification et exports après correction par l'investigateur et approbation humaine.
- [x] Distinguer dans le bilan formulations, décisions de revue et résultats du solveur.

Validation : observations synthétisées dans la [mémoire technique](memory.md#bilan-des-fonctionnalités-et-validations-v1). **Statut : parcours observés et bilan réalisés.**

**V1 — Terminée sur son périmètre fonctionnel et technique.**
