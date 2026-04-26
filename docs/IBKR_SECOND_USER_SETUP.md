# IBKR Read-Only Second-User Setup

**Zweck**: Mobile App nutzen (Account-Stand, Statements, Trade-Historie ansehen)
**ohne** den Bot rauszuhauen. Real-Money only — Paper-Account hat keine User-Hierarchie.

**Wann einrichten**: Sobald Real-Account aktiv ist (geplant W4: 18.-24.05.2026)

---

## Voraussetzungen

- Master-Account-Login zu IBKR
- Real-Account aktiviert (U-Prefix, NICHT DU-Paper)
- 2FA am Master-Account bereits aktiv (Empfehlung)
- ~10 Minuten Zeit

---

## Schritt-für-Schritt

### 1. In Client Portal einloggen
- https://www.interactivebrokers.com -> Login (Master-Account)
- 2FA bestätigen wenn aktiv

### 2. Settings öffnen
- Oben rechts: Profil-Icon -> "Settings"
- ODER direkt: https://www.interactivebrokers.com/portal/?action=ACCT_MGMT_MAIN

### 3. User-Management aufrufen
- Linke Navigation: "Account Settings"
- Suchfeld: "Users" eingeben
- Klick auf "Users & Access Rights"

### 4. Neuen User anlegen
- Button "Add User" / "+ Benutzer hinzufuegen"
- Felder:

```
Username:    cbaumann_view  (frei waehlbar, NICHT identisch mit Master)
First Name:  Carlos
Last Name:   Baumann
Email:       deine-eigene-email@adresse.ch
```

### 5. Permissions konfigurieren — KRITISCH

Read-Only-Setup:

| Permission | Setting |
|---|---|
| View Account Information | ✅ AKTIVIEREN |
| View Trade Confirmations | ✅ AKTIVIEREN |
| View Activity Statements | ✅ AKTIVIEREN |
| View Tax Documents | ✅ AKTIVIEREN |
| Trade Stocks/Options/Futures | ❌ NICHT AKTIVIEREN |
| Place Orders | ❌ NICHT AKTIVIEREN |
| Modify/Cancel Orders | ❌ NICHT AKTIVIEREN |
| Funds Transfer (Deposit/Withdraw) | ❌ NICHT AKTIVIEREN |
| Modify Account | ❌ NICHT AKTIVIEREN |
| Add/Remove Users | ❌ NICHT AKTIVIEREN |

Goldene Regel: Wenn unsicher -> deaktiviert lassen. Du willst wirklich
NUR Read-Access fuer diesen User.

### 6. Bestaetigung & 2FA
- IBKR sendet Verification-Email an die angegebene Adresse
- Email-Link klicken
- Initial-Passwort fuer neuen User wird vergeben
- Beim ersten Login: 2FA fuer den neuen User einrichten (separate Token-App empfohlen, z.B. IBKR Mobile App in 2FA-Modus oder Google Authenticator als Backup)

### 7. Test des Setups
1. IBKR Mobile App auf Handy oeffnen
2. Wenn vorher mit Master eingeloggt: ausloggen
3. Login mit `cbaumann_view` + neuem Passwort + 2FA
4. Pruefen: Portfolio sichtbar? Trade-Historie sichtbar?
5. Pruefen: Buy/Sell-Buttons sind ausgegraut/disabled?
6. **Wichtig**: Bot-Container parallel pruefen — laeuft normal weiter?
   `ssh root@178.104.236.157 'docker logs investpilot --since 5m | grep abgeschlossen | tail -3'`
   Bot sollte UNGESTOERT weiterlaufen, weil andere User-Session.

---

## Was ist anders nach Setup?

**Vorher (nur Master-User):**
- Mobile-Login = Bot wird gekickt
- Bot reconnected nach 5-10 Min
- Annoying aber kein Schaden

**Nachher (mit Second-User):**
- Mobile-Login mit `cbaumann_view` = parallel zum Bot
- Bot bleibt stabil
- Du siehst alle Daten read-only
- **Trades nur via Bot oder via Master-Login moeglich**

---

## Edge Cases

### Was wenn ich versehentlich mit Master-User im Mobile einlogge?
Selber Effekt wie heute: Bot wird kurz gekickt, reconnected nach 5 Min. Ausloggen, mit Second-User wieder rein.

### Kann der Second-User die Master-Permissions aendern?
Nein. Second-User hat selbst keine "Modify Users"-Permission. Schutzgarantie.

### Was wenn Second-User-Passwort kompromittiert?
Geringer Schaden (nur Read). Trotzdem: Master-Login -> User Management -> Second-User loeschen, neu anlegen.

### Funktioniert IBKR Desktop TWS auch mit Second-User?
Ja. Aber irrelevant fuer dich (Bot nutzt IB Gateway, du brauchst nur Mobile).

---

## Notiz zur Reihenfolge

In W4 (18.-24.05.) gemaess Roadmap:
1. Master-Account 2FA aktivieren (Hard-Gate #6)
2. 2000 CHF einzahlen
3. **Erst DANN** Second-User anlegen (Permissions vererben Master-Settings)
4. Read-Only-Login testen
5. Cutover-Tag (Do 28.05.)

---

## Falls Probleme

- IBKR Helpdesk: +44 207 710 9333 (deutsch verfuegbar)
- Live-Chat im Client Portal (rechts unten)
- Knowledge Base: https://www.interactivebrokers.com/en/index.php?f=4181
