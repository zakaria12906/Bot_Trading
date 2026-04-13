# Comment tester le bot sur MT5 Strategy Tester — Guide pas à pas

## Étape 1 — Copier le fichier dans MT5

1. Ouvre MetaTrader 5
2. Clique sur **File → Open Data Folder**
3. Un dossier s'ouvre. Navigue dans : `MQL5\Experts\`
4. Copie le fichier `HedgedGrid.mq5` dans ce dossier
5. Retourne dans MT5

## Étape 2 — Compiler l'Expert Advisor

1. Dans MT5, clique sur **Tools → MetaQuotes Language Editor** (ou F4)
2. Dans le MetaEditor, ouvre le fichier : `Experts\HedgedGrid.mq5`
3. Clique sur **Compile** (ou F7)
4. Tu dois voir : `0 error(s), 0 warning(s)`
5. Ferme le MetaEditor et retourne dans MT5

## Étape 3 — Ouvrir le Strategy Tester

1. Dans MT5, clique sur **View → Strategy Tester** (ou Ctrl+R)
2. Le panneau du testeur apparaît en bas de l'écran

## Étape 4 — Configurer le test

### Onglet "Settings"

| Paramètre | Valeur à mettre |
|---|---|
| Expert Advisor | HedgedGrid |
| Symbol | XAUUSD (ou le nom du Gold chez ton broker) |
| Period | M1 (1 minute) — le plus précis |
| Date | De: 2023-04-01  À: 2026-04-01 (3 ans) |
| Modeling | Every tick based on real ticks |
| Deposit | 1000 |
| Currency | USD |
| Leverage | 1:100 ou 1:500 (selon ton broker) |

### Onglet "Inputs" (paramètres du bot)

| Paramètre | Valeur | Description |
|---|---|---|
| BaseLot | 0.01 | Lot de base |
| MaxLevels | 9 | Profondeur max |
| GridStep | 5.0 | Distance entre niveaux |
| BasketTP | 15.0 | Profit cible ($) |
| MagicNumber | 888001 | ID unique |
| SessionStart | 1 | Heure début UTC |
| SessionEnd | 22 | Heure fin UTC |
| Slippage | 30 | Slippage max |

## Étape 5 — Lancer le test

1. Clique sur **Start** (bouton vert en bas)
2. Le test commence — tu verras une barre de progression
3. Durée : 5 à 30 minutes selon la puissance de ton PC

## Étape 6 — Lire les résultats

Quand le test est fini, regarde les onglets :

### Onglet "Results"
- Chaque ligne = un trade fermé
- Colonne "Profit" = gain/perte par trade

### Onglet "Graph"
- Courbe bleue = évolution du solde
- Si la courbe monte = le bot gagne
- Si elle descend = le bot perd

### Onglet "Report"
Les métriques clés :

| Métrique | Ce qu'il faut regarder |
|---|---|
| Total Net Profit | Le profit total sur 3 ans |
| Profit Factor | Doit être > 1.5 (bon), > 3.0 (excellent) |
| Max Drawdown | La perte maximale temporaire |
| Win Rate | % de trades gagnants |
| Recovery Factor | Profit / Drawdown (plus c'est haut, mieux c'est) |

## Étape 7 — Optimiser les paramètres

1. Dans le Strategy Tester, coche **Optimization**
2. Onglet **Inputs** → coche les paramètres à optimiser :

| Paramètre | Start | Step | Stop |
|---|---|---|---|
| GridStep | 3.0 | 1.0 | 10.0 |
| BasketTP | 8.0 | 2.0 | 30.0 |
| MaxLevels | 5 | 1 | 9 |

3. Clique **Start**
4. MT5 teste TOUTES les combinaisons et te montre laquelle est la meilleure
5. Durée : 1 à 12 heures selon le nombre de combinaisons

## Scénarios à tester

### Test 1 — Configuration standard
```
GridStep=5.0  BasketTP=15.0  MaxLevels=9
```

### Test 2 — Conservateur (moins de risque)
```
GridStep=7.0  BasketTP=20.0  MaxLevels=6
```

### Test 3 — Agressif (plus de cycles)
```
GridStep=3.0  BasketTP=10.0  MaxLevels=9
```

### Test 4 — Très safe
```
GridStep=8.0  BasketTP=25.0  MaxLevels=5
```

## Notes importantes

- **M1 + Every tick** donne les résultats les plus réalistes
- Le premier test prend du temps car MT5 télécharge les données historiques
- Si le symbole s'appelle différemment (GOLD, XAUUSDm, etc.) → change-le
- Les résultats en backtest sont TOUJOURS meilleurs qu'en réel (pas de slippage réel, pas de requotes)
- Teste TOUJOURS en DEMO avant de passer en réel
