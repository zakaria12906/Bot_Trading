# Hedged Grid Bot — Guide Complet pour Débutant

## Qu'est-ce que ce bot ?

Un bot qui ouvre BUY + SELL en même temps sur le Gold (XAU/USD), puis ajoute
des positions avec des lots croissants quand le prix bouge. Quand le profit
net de toutes les positions atteint un seuil ($15), il ferme tout et recommence.

```
Séquence de lots : 0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11, 0.17, 0.25
```

---

## Structure du projet

```
Trading_view_Bot/
├── main.py              # Lance le bot LIVE sur MetaTrader 5
├── backtest.py          # Test sur données historiques (fonctionne sur Mac)
├── config.yaml          # Tous les paramètres du bot
├── .env.example         # Template pour les identifiants MT5
├── requirements.txt     # Dépendances Python
├── broker/
│   ├── base_broker.py   # Interface abstraite
│   └── mt5_connector.py # Connexion MetaTrader 5
├── core/
│   ├── lot_sequence.py  # Calcul de la séquence de lots
│   ├── basket.py        # Gestion du panier de positions
│   ├── engine.py        # Logique principale du bot
│   └── bot.py           # Orchestrateur multi-symboles
└── utils/
    └── logger.py        # Logs console + fichier
```

---

## ÉTAPE 1 — Backtest sur Mac (maintenant, sans MT5)

### 1.1 Ouvrir le Terminal

Appuie sur `Cmd + Espace`, tape `Terminal`, appuie `Enter`.

### 1.2 Aller dans le dossier du bot

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/mohamed/Trading_view_Bot
```

### 1.3 Créer un environnement Python isolé

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Tu verras `(.venv)` apparaître au début de la ligne. Ça veut dire que tu es
dans l'environnement isolé.

### 1.4 Installer les dépendances du backtest

```bash
pip install yfinance pandas numpy matplotlib
```

### 1.5 Lancer le backtest

```bash
# Test de base — Gold, 24 mois
python backtest.py

# Personnaliser les paramètres
python backtest.py --months 24 --step 5.0 --tp 15.0

# Tester différents paramètres
python backtest.py --step 4.0 --tp 12.0       # step plus serré, TP plus bas
python backtest.py --step 7.0 --tp 20.0       # step plus large, TP plus haut
python backtest.py --max-levels 6              # limiter à 6 niveaux (plus safe)
```

### 1.6 Lire les résultats

Après le backtest, tu auras :
- **Dans le terminal** : rapport complet (win rate, P/L, drawdown)
- **backtest_hedged_grid.png** : graphique avec courbe d'équité
- **backtest_hedged_grid_trades.csv** : détail de chaque cycle

**Ce qu'il faut regarder :**

| Métrique | Bon signe | Mauvais signe |
|---|---|---|
| Win rate | > 90% | < 80% |
| Profit factor | > 5 | < 2 |
| Max drawdown | < $50 | > $200 |
| Avg duration | < 200 bars | > 500 bars |
| Level 7-8 % | < 10% | > 30% |

---

## ÉTAPE 2 — Installer MetaTrader 5 sur Mac

MT5 est un logiciel Windows. Sur Mac, tu as besoin d'une solution pour
le faire tourner.

### Option A — Parallels Desktop (recommandé, ~100€)

1. **Télécharger Parallels** : [parallels.com](https://www.parallels.com)
2. **Installer** : ouvre le `.dmg` et suis les instructions
3. **Windows 11** : Parallels va te proposer d'installer Windows automatiquement
4. **Attendre** : l'installation prend 15-30 minutes

### Option B — UTM (gratuit, plus lent)

```bash
brew install --cask utm
```

1. Télécharger l'ISO Windows ARM : [microsoft.com/software-download/windowsinsiderpreviewarm64](https://www.microsoft.com/software-download/windowsinsiderpreviewarm64)
2. Ouvrir UTM → New → Virtualize → Windows → sélectionner l'ISO
3. Donner 4 GB de RAM et 60 GB de disque
4. Installer Windows en suivant les étapes

### Option C — VPS Cloud (pour trading 24/7, ~10-30€/mois)

Providers : Contabo, Vultr, AWS EC2, ForexVPS

---

## ÉTAPE 3 — Installer MT5 + Python dans Windows

**Tout ce qui suit se fait DANS Windows (Parallels/UTM/VPS).**

### 3.1 Installer MetaTrader 5

1. Ouvrir le navigateur dans Windows
2. Aller sur le site de ton broker (ex: Exness, XM, IC Markets)
3. Télécharger MetaTrader 5
4. Installer et lancer
5. Se connecter avec un **compte DEMO** (très important : DEMO d'abord)

### 3.2 Configurer MT5

1. Aller dans **Tools → Options → Expert Advisors**
2. Cocher :
   - ✅ Allow algorithmic trading
   - ✅ Allow DLL imports
3. Cliquer OK

### 3.3 Installer Python dans Windows

1. Télécharger Python 3.11 : [python.org/downloads](https://python.org/downloads)
2. **IMPORTANT** : cocher ✅ "Add Python to PATH" pendant l'installation
3. Ouvrir **PowerShell** (chercher dans le menu démarrer)
4. Vérifier :

```powershell
python --version
```

Tu dois voir `Python 3.11.x`

### 3.4 Installer les dépendances du bot

```powershell
pip install MetaTrader5 pyyaml python-dotenv
```

---

## ÉTAPE 4 — Copier le bot dans Windows

### Depuis Parallels

Le disque Mac est accessible dans Windows à `\\Mac\Home`.

1. Ouvrir l'Explorateur de fichiers Windows
2. Aller à : `\\Mac\Home\Library\Mobile Documents\com~apple~CloudDocs\mohamed\Trading_view_Bot`
3. Copier tout le dossier dans `C:\Users\[ton_nom]\Documents\Trading_view_Bot`

### Depuis UTM ou VPS

```powershell
# Dans PowerShell Windows
git clone https://github.com/zakaria12906/MetaTrader_Bot.git
cd MetaTrader_Bot
pip install MetaTrader5 pyyaml python-dotenv
```

---

## ÉTAPE 5 — Configurer le bot

### 5.1 Créer le fichier .env

Dans le dossier du bot, copie `.env.example` et renomme-le `.env` :

```powershell
copy .env.example .env
```

Ouvre `.env` avec le Bloc-notes et remplis :

```
MT5_LOGIN=12345678
MT5_PASSWORD=ton_mot_de_passe
MT5_SERVER=Exness-MT5Trial
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

**Pour trouver ces infos :**
- `MT5_LOGIN` : ton numéro de compte (visible dans MT5 en haut)
- `MT5_PASSWORD` : le mot de passe de ton compte trading
- `MT5_SERVER` : visible dans MT5 → File → Login to Trade Account
- `MT5_PATH` : clic droit sur l'icône MT5 → Properties → Target

### 5.2 Vérifier config.yaml

Le fichier est déjà configuré pour le Gold. Les paramètres importants :

```yaml
symbols:
  XAUUSDs:                    # ← le nom du symbole chez ton broker
    enabled: true
    base_lot: 0.01            # ← taille minimum (ne change pas pour commencer)
    max_levels: 9             # ← profondeur maximum de la grille
    grid_step: 5.0            # ← distance entre les niveaux (5 points Gold)
    basket_tp: 15.0           # ← profit cible par cycle ($15)
```

**IMPORTANT** : Le nom du symbole peut varier selon ton broker :
- Exness : `XAUUSDm` ou `XAUUSD`
- IC Markets : `XAUUSD`
- XM : `GOLD`

Vérifie dans MT5 quel est le nom exact et mets-le dans `config.yaml`.

---

## ÉTAPE 6 — Premier test (DEMO)

### 6.1 Lancer MT5

1. Ouvre MetaTrader 5
2. Connecte-toi à ton compte **DEMO**
3. Vérifie que tu vois le prix du Gold bouger en temps réel

### 6.2 Lancer le bot

Ouvre PowerShell dans le dossier du bot :

```powershell
cd C:\Users\[ton_nom]\Documents\Trading_view_Bot
python main.py
```

Tu dois voir :

```
Hedged Grid Bot starting
Connected — Exness-MT5Trial | acct 12345678 | balance 10000.00 USD
Engine registered: XAUUSDs (magic 888001)
XAUUSDs lot sequence: [0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11, 0.17, 0.25]
XAUUSDs engine started | step=5.00 | tp=15.00 | levels=9
```

### 6.3 Surveiller

- Regarde dans MT5 : des positions vont apparaître dans l'onglet "Trade"
- Les logs sont dans `logs/hedged_grid.log`
- Pour arrêter : appuie `Ctrl + C` dans PowerShell

---

## ÉTAPE 7 — Tester sur 3 ans de données

Le backtest avec yfinance te donne ~24 mois max (limite de l'API gratuite).
Pour 3 ans complets, tu as deux options :

### Option A — Backtest yfinance (gratuit, ~24 mois max)

```bash
# Sur ton Mac
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/mohamed/Trading_view_Bot
source .venv/bin/activate
python backtest.py --months 36
```

Note : yfinance retourne souvent ~24 mois de données 1h pour Gold.
C'est suffisant pour tester.

### Option B — Strategy Tester de MT5 (3 ans+, données complètes)

MT5 a un testeur de stratégie intégré, mais il nécessite un EA (Expert Advisor)
en MQL5. Le bot Python ne peut pas être utilisé directement dans le Strategy Tester.

Pour tester sur 3 ans+ avec les données MT5, tu peux :

1. Lancer le bot en mode DEMO pendant 1-3 mois en temps réel
2. Analyser les résultats dans les logs et l'historique MT5

---

## Paramètres à tester

| Scénario | step | tp | max_levels | Risque |
|---|---|---|---|---|
| Conservateur | 7.0 | 20.0 | 6 | Faible |
| **Standard** | **5.0** | **15.0** | **9** | **Moyen** |
| Agressif | 3.0 | 10.0 | 9 | Élevé |

Lance le backtest avec chaque scénario :

```bash
# Conservateur
python backtest.py --step 7.0 --tp 20.0 --max-levels 6

# Standard
python backtest.py --step 5.0 --tp 15.0

# Agressif
python backtest.py --step 3.0 --tp 10.0
```

Compare les résultats et choisis celui qui te convient.

---

## Checklist de sécurité

- [ ] TOUJOURS commencer en compte DEMO
- [ ] JAMAIS augmenter le lot sans 1 mois de résultats
- [ ] JAMAIS laisser tourner pendant NFP / FOMC (couper le bot)
- [ ] Vérifier les logs chaque jour
- [ ] Si drawdown > $100 sur un compte $1000 → couper le bot
- [ ] Ne JAMAIS mettre d'argent que tu ne peux pas perdre

---

## Commandes de référence rapide

```bash
# === SUR MAC (backtest) ===
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/mohamed/Trading_view_Bot
source .venv/bin/activate
python backtest.py --months 24

# === SUR WINDOWS (live) ===
cd C:\Users\[ton_nom]\Documents\Trading_view_Bot
python main.py

# === Arrêter le bot ===
Ctrl + C

# === Voir les logs en temps réel ===
# PowerShell Windows :
Get-Content logs\hedged_grid.log -Wait -Tail 50
```

---

## Calendrier des news à éviter

| Jour | Événement | Action |
|---|---|---|
| 1er vendredi du mois | NFP (Non-Farm Payrolls) | COUPER le bot la veille à 21h |
| 8× par an (check FOMC) | Décision taux Fed | COUPER le bot 2h avant |
| ~12ème de chaque mois | CPI (Inflation US) | COUPER le bot la veille à 21h |

Calendrier : [forexfactory.com](https://www.forexfactory.com/calendar)

Filtre les événements "High Impact" (drapeau rouge) sur USD.
