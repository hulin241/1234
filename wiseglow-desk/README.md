# WiseGlow Desk

Un bot Feishu (飞书 / Lark) che vive nel gruppo del team WiseGlow e prepara documenti di lavoro parlando in modo naturale: NBO, LOI, manifestazioni di interesse per terreni, mandati di vendita, proposte per banche, contratti PPA Light / EPC / O&M / DDS / procacciatore, offerte prodotto e qualsiasi altro documento o contratto. Gli si mandano materiali (PDF, Word, Excel, immagini), gli si spiega cosa serve, si chiedono modifiche in chat e lui produce una nuova versione. Si può anche discutere con lui dei contenuti e fargli domande.

Il "cervello" è Claude Fable 5.1 attraverso il **Claude Agent SDK**, con una sessione persistente per ogni chat. **Ogni documento passa da un unico renderer** che applica l'identità visiva WiseGlow (logo, colori, font, intestazione, piè di pagina, tabelle): il contenuto è libero, la presentazione è sempre la stessa.

## Come funziona

1. Il bot riceve i messaggi di Feishu tramite la **connessione lunga** (WebSocket): niente server pubblico, gira sul Mac sempre acceso dell'ufficio come LaunchAgent.
2. Ogni messaggio viene deduplicato, gli allegati vengono sempre scaricati in `workspace/inbox/<chat>/`, e il bot decide se il messaggio è rivolto a lui (chat privata, menzione, risposta a un suo messaggio, oppure `WISEGLOW_TRIGGER_MODE=all`).
3. I messaggi del gruppo non rivolti a lui restano in un buffer di contesto (ultimi 40) che viene passato al turno successivo, così capisce di cosa si parla senza rispondere a tutto.
4. Un turno = una richiesta a Claude (Agent SDK) con gli strumenti `wiseglow`: `render_document`, `get_document`, `list_documents`, `send_file`, `send_message`, `read_office_file`, `save_reference`, `update_brand`, `catalog`.
5. `render_document` valida il JSON del documento, salva una nuova versione in `workspace/documents/<id>/vN.*`, genera DOCX (python-docx) e PDF (LibreOffice) e li invia in chat.
6. Reazioni sul messaggio dell'utente: 👀 `OnIt` mentre lavora, `DONE` a fine turno, `ERROR` se qualcosa è andato storto (con una spiegazione in chat).

## Requisiti

- macOS con Python ≥ 3.11 (lo script di installazione lo cerca e, se manca, lo installa con Homebrew)
- [LibreOffice](https://www.libreoffice.org/) per i PDF (`brew install --cask libreoffice`); senza, escono solo i DOCX
- Font Sora (titoli) e Inter (testo), gli stessi del sito WiseGlow (`brew install --cask font-sora font-inter`); vedi `brand/fonts/README.md`
- Un'app Feishu con capacità Bot (sotto) e una chiave API Anthropic (sotto)

## Checklist: creare l'app Feishu

Nella console sviluppatori (飞书: https://open.feishu.cn/app — Lark: https://open.larksuite.com/app):

1. **创建企业自建应用** (Create custom app): dai un nome (es. "WiseGlow Desk") e un'icona.
2. **凭证与基础信息** (Credentials & Basic Info): copia **App ID** (`cli_…`) e **App Secret** nel file `.env`.
3. **添加应用能力** (Add capability) → **机器人** (Bot).
4. **权限管理** (Permissions) → **开通权限**. Permessi minimi:
   - `im:message` — inviare messaggi e risposte, reazioni, scaricare i file che gli utenti mandano
   - `im:message.p2p_msg:readonly` — ricevere i messaggi in chat privata
   - `im:message.group_at_msg:readonly` — ricevere i messaggi del gruppo in cui è menzionato
   - `im:resource` — caricare file e immagini (i PDF/DOCX generati)

   Opzionali:
   - `im:message.group_msg` — ricevere **tutti** i messaggi del gruppo anche senza menzione. È un permesso sensibile (敏感权限: serve 创建版本 + 提交审核 e l'approvazione dell'amministratore; in un 测试企业 non serve approvazione). Necessario per `WISEGLOW_TRIGGER_MODE=all` e per vedere i file mandati nel gruppo senza menzionare il bot.
   - `im:chat:readonly` — leggere le informazioni della chat.
5. **事件与回调** (Events & Callbacks) → **事件配置** → **订阅方式** → **使用长连接接收事件** (Use long connection). Feishu salva questa scelta solo se un client è collegato in quel momento: avvia prima il bot (`python run.py`) e poi salva. Quindi **添加事件** → 消息与群组 → **接收消息 v2.0** (`im.message.receive_v1`).
6. **版本管理与发布** → **创建版本** → **申请发布** → approvazione dell'amministratore. Capacità, permessi ed eventi hanno effetto solo dopo la pubblicazione. In **可用范围** (availability) includi le persone che useranno il bot.
7. Aggiungi il bot al gruppo: impostazioni del gruppo → **群机器人** → **添加机器人**.

Il bot non riceve mai i propri messaggi; la consegna degli eventi è "almeno una volta" e il bot deduplica per `message_id`.

## Configurazione: il file `.env`

Copia `.env.example` in `.env` e compila. Le variabili già presenti nell'ambiente hanno la precedenza sul file.

| Variabile | Obbligatoria | Default | Significato |
|---|---|---|---|
| `FEISHU_APP_ID` | sì | — | App ID dell'app Feishu (`cli_…`) |
| `FEISHU_APP_SECRET` | sì | — | App Secret |
| `FEISHU_DOMAIN` | no | `feishu` | `feishu` (open.feishu.cn) o `lark` (open.larksuite.com) |
| `ANTHROPIC_API_KEY` | sì | — | Chiave API Anthropic (vedi "Costi e autenticazione") |
| `WISEGLOW_MODEL` | no | `claude-fable-5-1` | Modello Claude |
| `WISEGLOW_EFFORT` | no | `high` | Profondità di ragionamento: `low` `medium` `high` `xhigh` `max` |
| `WISEGLOW_TRIGGER_MODE` | no | `mention` | `mention`: risponde se menzionato, in privato o in risposta a un suo messaggio; `all`: a ogni messaggio del gruppo |
| `WISEGLOW_ALLOW_BASH` | no | `false` | Dà all'agente lo strumento Bash |
| `WISEGLOW_MAX_BUDGET_USD` | no | `5.0` | Tetto di spesa in dollari per singolo turno |
| `WISEGLOW_MAX_TURNS` | no | `60` | Passaggi massimi (chiamate a strumenti) per turno |
| `WISEGLOW_MAX_PARALLEL_CHATS` | no | `2` | Chat elaborate in parallelo |
| `WISEGLOW_TURN_TIMEOUT` | no | `900` | Durata massima di un turno in secondi (minimo 30): oltre, il turno viene interrotto e la chat resta libera |
| `WISEGLOW_ALLOWED_CHATS` | no | vuoto | Elenco di `chat_id` (`oc_…`) separati da virgola; vuoto = tutte le chat in cui è presente |
| `WISEGLOW_WORKSPACE` | no | `workspace` | Cartella di lavoro (allegati, documenti, sessioni, log) |
| `WISEGLOW_BRAND` | no | `brand/brand.yaml` | File dell'identità visiva |
| `WISEGLOW_LOG_LEVEL` | no | `INFO` | `DEBUG` `INFO` `WARNING` `ERROR` |
| `WISEGLOW_SOFFICE` | no | — | Percorso di `soffice` se LibreOffice non è in `/Applications` né nel `PATH` |

I percorsi relativi sono risolti rispetto alla cartella del progetto, non alla cartella corrente.

## Installazione sul Mac

```bash
git clone https://github.com/hulin241/1234.git ~/wiseglow-desk-repo
cd ~/wiseglow-desk-repo/wiseglow-desk
cp .env.example .env && chmod 600 .env   # compila FEISHU_APP_ID, FEISHU_APP_SECRET, ANTHROPIC_API_KEY
bash install-mac.sh
```

Il `.env` contiene segreti: `chmod 600` lo rende leggibile solo dal tuo utente (lo script lo fa comunque da solo).

Lo script (da eseguire nel Terminale del Mac, non via SSH):

1. controlla macOS, Python ≥ 3.11 (se manca: `brew install python@3.11`), LibreOffice e i font Sora/Inter;
2. crea `.venv` e installa `requirements.txt`;
3. verifica che `.env` esista (altrimenti lo copia da `.env.example` e si ferma con le istruzioni) e che sia leggibile solo dal tuo utente (`chmod 600`);
4. esegue `python run.py --check` (configurazione, brand, LibreOffice, credenziali Feishu, chiave Anthropic);
5. installa il LaunchAgent `com.wiseglow.desk`: parte al login, si riavvia entro 30 s se si ferma, log in `~/Library/Logs/wiseglow-desk.log`.

```bash
bash install-mac.sh --status      # stato del servizio + ultime righe di log
bash install-mac.sh --stop        # ferma il servizio senza disinstallarlo (--start per riavviarlo)
bash install-mac.sh --uninstall   # rimuove il servizio (progetto e workspace restano)
bash install-mac.sh --dry-run     # mostra cosa farebbe senza cambiare nulla
```

Per provarlo a mano senza LaunchAgent:

```bash
.venv/bin/python run.py --check                 # diagnostica in italiano, esce con 0/1
.venv/bin/python run.py --render samples/nbo.json   # genera DOCX+PDF di prova in workspace/render/ (senza Feishu)
bash install-mac.sh --stop                      # ferma il servizio, se installato (vedi sotto)
.venv/bin/python run.py                         # avvia il bot (Ctrl+C per fermarlo)
bash install-mac.sh --start                     # al termine, riavvia il servizio
```

**Prima di avviare `run.py` a mano ferma il servizio**, altrimenti Feishu distribuisce i messaggi a caso tra le due istanze: la connessione lunga è in modalità cluster, ogni messaggio arriva a **una sola** delle istanze collegate, e le due condividerebbero anche i file di sessione delle chat. `bash install-mac.sh --stop` equivale a `launchctl bootout gui/$(id -u)/com.wiseglow.desk` (in alternativa `bash install-mac.sh --uninstall`); alla fine `bash install-mac.sh --start` (oppure di nuovo `bash install-mac.sh`).

Il Mac deve restare acceso e connesso: in stop il bot non risponde (Impostazioni di Sistema → Batteria → disattiva lo stop automatico). Il LaunchAgent parte **al login**, non all'accensione: dopo un riavvio serve un login (o l'accesso automatico).

## Uso quotidiano nel gruppo

Menziona il bot (`@WiseGlow Desk …`) o scrivigli in privato. Non c'è una procedura da seguire: si parla come con un collega, in italiano, cinese o inglese.

- «@WiseGlow Desk preparami una NBO per il terreno di Lecce, 12 ettari, 1,2 milioni, controparte Rossi Agricola» → arriva un PDF con la grafica WiseGlow. I dati mancanti che cambierebbero il documento (prezzo, controparte, date) li chiede una volta sola; per il resto usa segnaposto visibili tipo `[DA CONFERMARE: superficie]`.
- Manda un file e poi chiedi: «questa è la LOI che abbiamo usato l'anno scorso, tienila come modello» → la salva tra i riferimenti e ne riusa struttura e clausole. Nei gruppi, un file mandato senza menzione viene comunque scaricato: basta menzionarlo dopo («@WiseGlow Desk usa il PDF di prima»).
- Modifiche: «nella NBO di Lecce porta il prezzo a 1,3 milioni e aggiungi 60 giorni di esclusiva» → nuova versione dello stesso documento, con due righe su cosa è cambiato. «Mandamela in Word» → DOCX.
- Altri tipi: «mi serve un contratto di procacciatore per Bianchi», «una proposta per la banca sul progetto Puglia», «un'offerta per 200 moduli da 550 W»; o un documento senza catalogo («un verbale di sopralluogo»).
- Domande e confronto: «cosa ne pensi della clausola di esclusiva?», «riassumi il contratto allegato», «cosa manca in questa LOI?» → risponde senza rifare il documento finché non glielo chiedi.

Comandi gestiti dal bot senza Claude (nei gruppi vanno menzionati: `@WiseGlow Desk /status`):

- `/reset` — chiude la conversazione di questa chat e riparte da zero (i documenti restano);
- `/status` — sessione attiva, documenti creati in questa chat, costo dell'ultimo turno, richieste in coda;
- `/help` — breve guida.

Le versioni stanno in `workspace/documents/<document_id>/v1.json, v1.docx, v1.pdf, v2…`; l'indice in `workspace/documents/index.json`. La memoria del bot tra una conversazione e l'altra (preferenze, clausole standard, decisioni) è `workspace/knowledge/NOTES.md`, i modelli salvati in `workspace/knowledge/templates/`.

## Brand: logo, colori, font

Tutta l'identità visiva sta in `brand/`:

- `brand/logo.png` — il wordmark ufficiale "WiseGlow · AI + ENERGY" (PNG trasparente, preso da wiseglow.com). Per cambiarlo sostituisci il file (o mandalo in chat: «usa questo come logo»). Larghezza in `brand.yaml` → `logo.width_cm`. Se il file manca, nell'intestazione compare il nome dell'azienda.
- `brand/brand.yaml` — dati aziendali (ragione sociale, indirizzo, P.IVA, contatti: il bot li cita nei documenti e non li inventa mai), colori (`primary` per titoli e tabelle, `secondary` per la linea accento e i riquadri), font, margini, piè di pagina, etichette per lingua. Modifica i valori, non le chiavi; il renderer rilegge il file a ogni documento.
- `brand/fonts/README.md` — quali font servono (Sora per i titoli, Inter per il testo, PingFang SC per il cinese) e come installarli con Homebrew. Il DOCX indica solo il nome del font: se non è installato sul Mac, LibreOffice sostituisce e l'aspetto cambia un po'.

Per vedere il risultato: `python run.py --render samples/nbo.json` (anche `loi.json`, `interesse_terreno.json`). Il bot può cambiare `brand.yaml` con lo strumento `update_brand` solo se glielo chiedi esplicitamente.

## Costi e autenticazione

Serve una **chiave API Anthropic** (https://console.anthropic.com, pagamento a consumo) in `ANTHROPIC_API_KEY`. La documentazione Anthropic è esplicita: «Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Please use the API key authentication methods» — quindi l'abbonamento claude.ai non si può usare per questo bot.

Claude Fable 5.1 costa **10 $ per milione di token in ingresso e 50 $ per milione in uscita**. Un documento medio con lettura di un paio di allegati costa in genere pochi dollari; `WISEGLOW_MAX_BUDGET_USD` (default 5 $) ferma un singolo turno che superasse il tetto e il bot lo dice in chat. `/status` mostra il costo dell'ultimo turno. Per spendere meno: `WISEGLOW_EFFORT=medium` per le richieste di routine, allegati più piccoli, `/reset` quando si cambia argomento (una sessione lunga porta con sé tutto il contesto).

## Risoluzione dei problemi

- **`run.py --check` dice "Feishu: impossibile leggere le informazioni del bot"** — App ID/Secret sbagliati, dominio sbagliato (`feishu` vs `lark`) o app senza capacità Bot pubblicata.
- **Il bot è avviato ma non risponde nel gruppo** — è stato aggiunto al gruppo? Lo stai menzionando? La versione dell'app è pubblicata e approvata? L'evento `im.message.receive_v1` è sottoscritto con la connessione lunga? Con `WISEGLOW_ALLOWED_CHATS` valorizzato, il `chat_id` del gruppo è nell'elenco?
- **Non salva "使用长连接接收事件"** — Feishu accetta l'impostazione solo mentre un client è collegato: avvia `python run.py` e poi salva.
- **Vede solo i messaggi con menzione** — è il comportamento con i permessi minimi. Per tutti i messaggi serve `im:message.group_msg` (approvazione admin) e `WISEGLOW_TRIGGER_MODE=all`.
- **"PDF non generato (LibreOffice assente)"** — installa LibreOffice (`brew install --cask libreoffice`) o imposta `WISEGLOW_SOFFICE`. I DOCX arrivano comunque.
- **I titoli non sono in Sora / il testo non è in Inter** — font non installati sul Mac: `brew install --cask font-sora font-inter`, poi riavvia il bot.
- **"ho superato il budget massimo per un singolo turno"** — richiesta troppo grande: spezzala, oppure alza `WISEGLOW_MAX_BUDGET_USD`.
- **Risposte lente** — Fable 5.1 su un contratto lungo può impiegare qualche minuto; il bot manda un avviso di una riga quando prevede di metterci più di un minuto. `WISEGLOW_EFFORT=medium` accelera.
- **Il servizio si riavvia in continuazione** — `bash install-mac.sh --status` mostra le ultime righe di log; se ci sono problemi di configurazione (`Configurazione non valida`, `ANTHROPIC_API_KEY assente`, `Brand non valido`), correggi `.env` o `brand/brand.yaml` e rilancia lo script. Per vedere l'errore dal vivo, **prima ferma il servizio**, altrimenti Feishu distribuisce i messaggi a caso tra le due istanze: `bash install-mac.sh --stop` (oppure `launchctl bootout gui/$(id -u)/com.wiseglow.desk`), poi `.venv/bin/python run.py`; al termine `bash install-mac.sh --start`.
- **`Brand non valido` / `--check` dice che `brand.yaml` non è YAML valido** — un'indentazione o una virgoletta sbagliata nel file: il messaggio indica riga e colonna. Correggi il file (o ripristinalo da git) e riavvia.
- **Il Mac va in stop** — disattiva lo stop automatico; con il coperchio chiuso il portatile va comunque in stop.
- **Le sessioni Claude spariscono dopo un aggiornamento** — la sessione viene ricreata da zero automaticamente; i documenti e la memoria in `workspace/knowledge/` non si perdono.

## Sviluppo

```bash
pip install -r requirements-dev.txt
pytest -q                     # nessuna chiamata di rete: Feishu e Claude sono simulati
```

Struttura: `wiseglow_desk/feishu/` (client REST, eventi, connessione lunga), `wiseglow_desk/agent/` (sessioni, prompt, strumenti MCP, cervello), `wiseglow_desk/docgen/` (modello del documento, brand, renderer DOCX, PDF, archivio versioni), `wiseglow_desk/bot.py` (il collegamento tra le parti), `run.py` (avvio). Commenti e docstring in inglese, messaggi per gli utenti in italiano.
