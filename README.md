# Restaurant Phone Agent

MVP per un agente vocale che risponde alle chiamate del ristorante, parla con il cliente in italiano e raccoglie richieste semplici.

## Cosa fa

- Risponde a una chiamata Twilio tramite `/twilio/inbound`.
- Collega l'audio della chiamata a OpenAI Realtime.
- Risponde su orari, indirizzo e menu.
- Raccoglie richieste di prenotazione senza confermarle definitivamente.
- Passa al personale i casi delicati come allergie, reclami o richieste fuori policy.

## Configurazione

1. Copia `.env.example` in `.env`.
2. Inserisci `OPENAI_API_KEY`.
3. Crea l'ambiente Python e installa le dipendenze:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install fastapi "uvicorn[standard]" websockets python-dotenv
```

4. Avvia il server:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

5. Esponi il server con ngrok:

```bash
ngrok http 8000
```

6. Metti l'URL pubblico in `.env` come `PUBLIC_BASE_URL`.
7. In Twilio, configura il webhook voce del numero su:

```text
https://TUO-DOMINIO/twilio/inbound
```

## Personalizzazione ristorante

Aggiorna `data/restaurant.json` con:

- nome reale
- indirizzo
- numero di telefono
- orari
- piatti principali
- regole su prenotazioni, asporto e allergie

## Note operative

Questo MVP raccoglie richieste, ma non scrive ancora su calendario, WhatsApp o gestionale. Il passo successivo naturale e' collegare `create_reservation_request` a un foglio Google, email, SMS o software prenotazioni.

## Deploy stabile

Per evitare tunnel temporanei, pubblica l'app su un servizio sempre acceso.

Opzione consigliata per iniziare: Render Web Service.

Impostazioni Render:

```text
Build command: pip install -r requirements.txt
Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Health check path: /health
```

Variabili ambiente da impostare:

```env
OPENAI_API_KEY=...
REALTIME_MODEL=gpt-realtime-mini
RESTAURANT_CONFIG=data/restaurant.json
STAFF_TRANSFER_PHONE=+12019954521
STAFF_ACCESS_TOKEN=un-token-lungo-da-inventare
RESERVATION_NOTIFY_EMAIL=ms@solaosteria.com
```

`PUBLIC_BASE_URL` e' opzionale su un server stabile: se non e' impostato, l'app usa automaticamente il dominio pubblico della richiesta Twilio.

Dopo il deploy, configura Twilio con:

```text
https://TUO-SERVIZIO.onrender.com/twilio/inbound
```

Metodo: `POST`.

## Prenotazioni raccolte

Le richieste confermate dal cliente vengono salvate in:

```text
data/reservation_requests.jsonl
```

Per vedere le ultime richieste mentre il server e' acceso:

```text
https://TUO-DOMINIO/reservations?token=IL_TUO_STAFF_ACCESS_TOKEN
```

Se configuri SMTP nel file `.env`, ogni nuova richiesta invia anche una email allo staff:

```env
STAFF_ACCESS_TOKEN=un-token-lungo-da-inventare
RESERVATION_NOTIFY_EMAIL=ms@solaosteria.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM_EMAIL=...
```

Nota: la richiesta resta sempre `pending_staff_confirmation`. Isabel non conferma disponibilita' reale.

## Trasferimento allo staff

Isabel puo' decidere di trasferire la chiamata quando serve una persona reale:

- richiesta esplicita di parlare con staff, manager, proprietario o chef
- cliente arrabbiato o reclamo
- allergie serie, celiachia o sicurezza ingredienti
- modifica/cancellazione di una prenotazione esistente
- disponibilita' reale tavoli
- gruppi da 10 persone in su, eventi privati, buyout
- pagamenti, rimborsi, gift card, oggetti smarriti, accessibilita', lavoro o fornitori

Per abilitare il trasferimento, aggiungi in `.env`:

```env
STAFF_TRANSFER_PHONE=+12018575100
```

Se `STAFF_TRANSFER_PHONE` e' vuoto, Isabel raccoglie nome, telefono e messaggio per follow-up.
