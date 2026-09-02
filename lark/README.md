# Quali business unit possiamo vedere su Lark?

## Risposta breve

Tre cose, in ordine di importanza:

1. **Su Lark non esiste un oggetto chiamato "business unit"**. Non c'è nessuna voce di menu, nessun campo e nessuna risorsa API con quel nome. Quando in azienda si dice "business unit" si intende una di due cose diverse: un **Dipartimento** (`Department` / 部门) chiamato come una BU, oppure una **Unità** (`Unit` / 单位), che è un oggetto formale separato, a pagamento, pensato per le controllate.
2. **Quello che vedi tu è già scritto a schermo**: l'albero che ti compare sotto **Contatti → Organizzazione** (`Contacts > Organization` / 通讯录 > 组织架构) **è** il tuo perimetro di visibilità. Non esiste una schermata "i miei permessi" per gli utenti normali.
3. **Solo un amministratore può leggere le regole configurate.** Un dipendente non può interrogarle, nemmeno via API. Se non sei admin, la risposta esatta te la deve dare l'IT — sotto c'è il testo da inoltrargli.

---

## 1. Che cosa esiste davvero al posto della "business unit"

| Oggetto | Nome IT/EN/ZH | Che cos'è | Piano |
|---|---|---|---|
| Dipartimento | Department / 部门 | Nodo dell'albero organizzativo. È il modo più comune di modellare una BU: la documentazione Lark usa come esempi di **dipartimento** proprio "Entertainment BU" (EN) e 零售事业部 / "BU Retail" (ZH). | Tutti |
| Unità | Unit / 单位 | Oggetto **separato dall'albero**, pensato per controllate e filiali ("subsidiary-level permission isolation"). Non contiene persone: si popola **associando dipartimenti**. Ha solo tre campi: `unit_id`, `name`, `unit_type` (testo libero, 1–100 caratteri). Gli esempi nella documentazione API sono letteralmente `子公司`, `事业部`, **`BU`**, con `name` = 消费者事业部 e `unit_id` = `BU121`. | **Solo Enterprise / 旗舰版** (a pagamento) |

Vincoli sulle Unità, verificati:

- **Un dipartimento può essere associato a una sola unità.** Se lo assegni a una seconda, viene rimosso automaticamente dalla nuova.
- Le unità **non sono annidabili** e non compaiono come nodi dell'albero organizzativo.
- Max **1.000 dipartimenti** per unità.
- Il `unit_type` **non è modificabile** dopo la creazione.
- Numero massimo di unità per tenant: **le fonti si contraddicono.** La documentazione API attuale dice 1.000 (errore `43059`: 单租户内单位数量不得超过 1,000 个), la FAQ dell'articolo di help dice ancora 100. Fidati del limite API e non citare il 100 come limite certo.
- Se il piano a pagamento scade, le unità già create continuano a funzionare: l'admin può cancellarle ma non aggiungerne.

Da non confondere: collegare **due tenant distinti** è un'altra funzione, chiamata **Trusted party** (internazionale) / 关联组织 (Cina).

---

## 2. Chi decide che cosa vedi

Un'unica impostazione, lato amministratore:

**Lark Admin Console → Security → Member Permissions → Visibility scope of organization**
(飞书管理后台 → 安全合规 → 成员权限 → 组织架构可见范围)

> La label della sezione varia tra le revisioni della documentazione: `Security` in inglese, 安全 in articoli vecchi, 安全合规 in quelli attuali. Cerca quella che trovi.

### Regola principale (Main rule / 主规则) — esattamente 4 opzioni

| Opzione | Effetto |
|---|---|
| Members can see everyone / 成员可见所有人 | Tutti vedono tutti. |
| **Members can see their unit / 成员可见本单位** | Ognuno vede solo l'organigramma della **propria unità**. Richiede la funzione Unità attiva e la scelta di **quale `unit_type`** si applica. ⚠️ Chi non appartiene a nessuna unità, in assenza di regole supplementari, **non vede nessuno**. |
| Members can see their department / 成员可见本部门 | Ognuno vede il proprio dipartimento. Nell'articolo cinese c'è anche una casella "同时可见本部门的下级部门" (vedi anche i sotto-dipartimenti); nell'articolo inglese quella casella non è documentata. |
| Members can't see anyone / 成员不可见任何人 | Nessuno vede nessuno. |

Nelle prime tre opzioni, "i responsabili di dipartimento vedono il proprio dipartimento" (部门负责人默认可见自己负责的部门) è **obbligatorio e non disattivabile**. Solo nella quarta diventa una casella opzionale.

### Regole supplementari (Supplementary rules / 补充规则)

Due livelli, alta e bassa priorità. Ogni regola è: **Soggetto** (membro / gruppo utenti / dipartimento) + **Visibilità** (può vedere / non può vedere) + **Oggetto** (membro / dipartimento).

⚠️ **Le Unità non compaiono tra i tipi ammessi come soggetto o oggetto** in nessun articolo. Le unità funzionano solo come opzione della regola principale. (La documentazione non lo nega esplicitamente, ma non le elenca mai.)

---

## 3. Come scoprire cosa vedete voi

### Se non sei amministratore

1. **Apri Contatti → Organizzazione** (通讯录 → 组织架构). Quello che riesci a espandere è il tuo perimetro, per definizione. Fai uno screenshot: è la risposta empirica alla domanda.
2. **Controlla la tua scheda**: clicca la tua foto profilo → **Profile**. Il campo **Dipartimento** c'è sempre (è tra i campi base non disattivabili: Notes, Bio, Avatar, Name, Department). Il campo **Unità** compare **solo se** un admin l'ha abilitato in `Organization > Field Management > Fields Display > Profile page`.
3. **Non usare la ricerca come prova.** Vedi la sezione Trappole.
4. Per la risposta esatta, chiedi all'IT (testo pronto in fondo).

### Se sei amministratore

Verifica prima di esserlo: foto profilo → **Administration** (internazionale) / 管理后台 (Cina); da browser `larksuite.com/admin` o `feishu.cn/admin`; da mobile, in **Workplace** cerca l'app **Suite Admin**. Possono entrare solo il creatore dell'azienda, i super amministratori e gli amministratori.

| Cosa vuoi sapere | Dove |
|---|---|
| Elenco delle **Unità** formali | `Organization > Unit` (组织架构 > 单位管理). Serve essere super admin o avere il permesso **Member and Department** (成员与部门). |
| Elenco dei **dipartimenti** | `Organization > Member and Department` |
| Le **regole di visibilità** attive | `Security > Member Permissions > Visibility scope of organization` |
| Se **X** vede **Y** | Nella stessa pagina, tab delle impostazioni del perimetro → **Visibility Verification Rules** (验证权限规则). Inserisci nome / email / telefono di soggetto e oggetto → **Verify** (查询). |
| Fin dove arrivi **tu** come admin | `Settings > Administrator Permissions > Administrator Role` (企业设置 > 管理员权限 → tab 管理员角色). Solo i super amministratori possono modificare permessi e management scope. |

⚠️ Due limiti reali di questi strumenti:

- **Visibility Verification Rules è a coppie e non è definitivo**: verifica un membro contro un altro, una coppia per volta, e la documentazione stessa descrive un caso in cui il suo risultato divergerebbe da ciò che i Contatti mostrano davvero.
- Il **management scope** di un amministratore esiste in quattro sole varianti — Member and department / Meeting room / User group / Application — ed è espresso per **dipartimenti**, mai per unità. **Non si può limitare un amministratore a una business unit.**

---

## 4. Trappole

- **Visibilità ≠ ricercabilità.** La ricerca è un'impostazione **separata**: `Security > Member Permissions > Search permissions` (搜索权限). La sua regola principale ha due sole opzioni — "No restrictions" (无限制) e "Consistent with visible scope of the organizational structure" (和组织架构可见范围一致) — più regole supplementari. Esiste anche un'opzione che **sfonda esplicitamente** il perimetro: 按部门搜人时，可通过组织内任何部门搜索成员 ("cercando per dipartimento, i membri possono cercare in qualsiasi dipartimento dell'organizzazione"). Quindi: *trovare* qualcuno con la ricerca non dimostra che la sua BU sia nel tuo perimetro, e non trovarlo non dimostra il contrario.
- **Il nome del dipartimento trasuda dalle schede profilo.** Il campo Dipartimento non è disattivabile, quindi nomi di BU possono comparire anche fuori dal tuo albero.
- **"Members can see their unit" può azzerare la visibilità** di chi non è in nessuna unità (vedi tabella sopra).
- Nessun report e nessuna API restituisce in blocco "tutti i dipartimenti che X può vedere". L'unica strada è camminare l'albero con il token dell'utente (sotto).

---

## 5. Via API

Le Unità **esistono** nell'API Contact v3, ma con un limite decisivo per questa domanda.

**Lettura unità** — scope `contact:unit:readonly` (获取单位信息), **solo `tenant_access_token`**, solo app interne (custom app). Rate limit 1000/min, 50/s.

```
GET  /open-apis/contact/v3/unit                  # elenco unità
GET  /open-apis/contact/v3/unit/:unit_id         # una unità
GET  /open-apis/contact/v3/unit/list_department  # dipartimenti associati a una unità
```

**Scrittura unità** — scope `contact:unit` (更新单位信息), rate limit 20/s:

```
POST  /open-apis/contact/v3/unit                  # crea
PATCH /open-apis/contact/v3/unit/:unit_id         # modifica
POST  /open-apis/contact/v3/unit/bind_department   # associa un dipartimento
```

⚠️ **Non esiste nessun endpoint sulle unità che accetti `user_access_token`.** Di conseguenza **non esiste nessuna API che risponda "quali unità può vedere questo utente"**. La visibilità, via API, passa solo dai dipartimenti:

| Endpoint | Token | Filtrato da |
|---|---|---|
| `GET /open-apis/contact/v3/scopes` | solo `tenant_access_token` | perimetro autorizzato **dell'app** (通讯录授权范围), non di un utente |
| `POST /open-apis/contact/v3/departments/search` | solo `user_access_token` | **perimetro di visibilità del chiamante**. Ma `query` è obbligatorio e cerca per nome: è una ricerca per parola chiave, non un'enumerazione |
| `GET /open-apis/contact/v3/departments/:department_id/children` | `user_access_token` o `tenant_access_token` | perimetro di visibilità del chiamante → **è questo che si cammina ricorsivamente** per enumerare l'albero visibile a un utente |
| `POST /open-apis/directory/v1/departments/filter` | `user_access_token` | attenzione: valida contro il **management scope dell'amministratore**, che è una terza nozione diversa |

---

## 6. Cosa resta da verificare nel vostro tenant

Nessuna di queste risposte si ricava dalla documentazione pubblica:

- **Siete su Lark internazionale (`larksuite.com`) o Feishu Cina (`feishu.cn`)?** Cambia label, articoli e disponibilità delle funzioni.
- **Che piano avete?** Le Unità sono solo Enterprise/旗舰版. Senza quel piano, le vostre "business unit" **sono** dipartimenti, e la domanda diventa "quali dipartimenti vediamo".
- **La funzione Unità è attivata e ci sono unità create?** L'opzione "Members can see their unit" è selezionabile solo in quel caso.
- **Usate Lark People / 飞书人事 (CoreHR)?** È l'**unico** posto dove potrebbe esistere un oggetto chiamato letteralmente "business unit": il prodotto HR definisce oggetti propri (公司/company, 成本中心/cost center, 序列/job family, 自定义组织/custom organization) accanto ai dipartimenti, con un proprio modello di permessi e ruoli separati (人事管理员 / 人事子管理员, ognuno con il proprio 管理范围 — nemmeno un super amministratore ha automaticamente i permessi HR). Questa documentazione non è stato possibile verificarla: va guardata nel tenant.
- **Le label in italiano.** L'help center Lark pubblica solo en-US, zh-CN, ja-JP e vi: le stringhe italiane dell'interfaccia non sono verificabili sulle fonti ufficiali. Le label qui sopra sono quelle inglesi e cinesi.

Non documentato da nessuna parte, da testare sul campo se serve:

- Se un dipartimento fuori dal tuo perimetro **sparisce** dall'albero o compare come nodo vuoto.
- Se le Unità sono navigabili nell'albero Contatti del client, o se restano solo un campo sulla scheda profilo e un criterio delle regole.
- Se una persona in due dipartimenti legati a due unità diverse appartenga a entrambe, e quale unità risolva la regola "Members can see their unit".

---

## 7. Testo da inoltrare all'IT / all'amministratore Lark

> Ciao, mi serve sapere esattamente quale perimetro dell'organigramma è visibile a noi su Lark. Dalla console di amministrazione, potresti dirmi:
>
> 1. In `Security > Member Permissions > Visibility scope of organization`: quale **regola principale** è attiva (everyone / their unit / their department / no one) e quali **regole supplementari** ci riguardano?
> 2. Usiamo le **Unità** (`Organization > Unit`)? Se sì, quali sono e quali dipartimenti sono associati a ciascuna? Se no, le nostre "business unit" sono dipartimenti e mi basta l'elenco dei dipartimenti di primo livello.
> 3. In `Security > Member Permissions > Search permissions`: la regola principale è "No restrictions" o "Consistent with visible scope of the organizational structure"?
> 4. Se serve una verifica puntuale, puoi usare **Visibility Verification Rules** su una coppia di persone concrete.

---

## Fonti

Help center (i corpi degli articoli sono resi via JavaScript: per leggerli serve l'HTML grezzo):

- [Admin | Create and manage units](https://www.larksuite.com/hc/en-US/articles/360048487829) · [管理员进行单位管理](https://www.feishu.cn/hc/zh-CN/articles/333548009177)
- [Admin | Manage the visibility of your organizational structure](https://www.larksuite.com/hc/en-US/articles/360048487831) · [管理员管理组织架构可见范围](https://www.feishu.cn/hc/zh-CN/articles/360049067480)
- [Admin | Set search permissions](https://www.larksuite.com/hc/en-US/articles/360048488475)
- [Admin | Configure members' profile pages](https://www.larksuite.com/hc/en-US/articles/360048487946)
- [Admin | Add administrators and create administrator roles](https://www.larksuite.com/hc/en-US/articles/360043595213)
- [管理员登录飞书管理后台](https://www.feishu.cn/hc/zh-CN/articles/876813189909)

Open platform:

- [Contact v3 — elenco risorse](https://open.feishu.cn/document/server-docs/contact-v3/resources?lang=zh-CN)
- [单位 / Unit — overview](https://open.feishu.cn/document/server-docs/contact-v3/unit/overview)
- [搜索部门 / Search department](https://open.feishu.cn/document/server-docs/contact-v3/department/search)
- [获取通讯录授权范围 / Get contacts scope](https://open.feishu.cn/document/server-docs/contact-v3/scope/list)
