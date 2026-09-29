# Font WiseGlow

I documenti usano i font indicati in `brand.yaml` (`fonts:`), gli stessi del sito
wiseglow.com e dei whitepaper ufficiali:

| Uso | Font | Fallback |
| --- | --- | --- |
| Titoli e intestazioni | **Sora** (SemiBold / Regular) | Arial |
| Testo | **Inter** | Arial |
| Cinese (w:eastAsia) | **PingFang SC** (di serie su macOS) | Noto Sans CJK SC |

Il DOCX indica solo il *nome* del font: viene usato al momento dell'apertura o
della conversione in PDF. Se il font non è installato sul Mac che genera i PDF,
LibreOffice sostituisce automaticamente con un font simile e l'aspetto cambia
leggermente. Per avere sempre l'identità corretta installa i font sul Mac.

## Installazione su macOS

```bash
# Sora e Inter (Google Fonts, licenza OFL)
brew install --cask font-sora font-inter

# PingFang SC è già presente su macOS. Su Linux: Noto Sans CJK SC
```

Dopo l'installazione riavvia il bot (o rilancia `install-mac.sh`), perché
LibreOffice legge l'elenco dei font all'avvio.

## Controllo

```bash
fc-list | grep -iE "sora|inter"          # se fontconfig è disponibile
python run.py --render samples/nbo.json   # genera un PDF di prova
```

I file `.ttf`/`.otf` NON vanno messi in questa cartella: i font si installano a
livello di sistema (Font Book). Questa cartella contiene solo le istruzioni.
